"""Tests for the defensive decoy-404 middleware.

App construction, session mocking, and AsyncClient usage are modeled on
tests/test_multi_event_routing.py.
"""

from __future__ import annotations

import gzip
import random

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from httpx import ASGITransport, AsyncClient

from flight_card_scanner.middleware.decoy_middleware import (
    BOMB_DECOMPRESSED_SIZE,
    BOMB_PROBABILITY,
    DECOY_MESSAGE,
    DECOY_MESSAGE_BYTES,
    build_decoy_response,
    decoy_404_middleware,
    is_exempt_path,
)


# ---------------------------------------------------------------------------
# Deterministic RNGs for forcing each branch
# ---------------------------------------------------------------------------


class _ForceBomb:
    """RNG whose random() always returns 0.0 (< BOMB_PROBABILITY => bomb)."""

    def random(self) -> float:
        return 0.0


class _ForcePlain:
    """RNG whose random() always returns 0.99 (>= BOMB_PROBABILITY => plain)."""

    def random(self) -> float:
        return 0.99


# ---------------------------------------------------------------------------
# App builder (modeled on test_multi_event_routing.py)
# ---------------------------------------------------------------------------


def _build_app(user=None, rng=None):
    """Build a minimal app with a session middleware + the decoy middleware.

    A couple of real routes are registered; any unmatched path 404s naturally.
    ``user`` is what the session-resolution-like middleware attaches to
    request.state.user. ``rng`` (optional) forces the decoy branch.
    """
    app = FastAPI()

    # Decoy middleware defined FIRST so the session middleware (added after)
    # runs outermost and populates request.state.user first, mirroring main.py.
    # We always drive the REAL production entrypoint (decoy_404_middleware),
    # threading the optional forced ``rng`` through it so the e2e tests exercise
    # the production code path (not a re-implementation) while still forcing the
    # plain/bomb branch deterministically.
    @app.middleware("http")
    async def _decoy(request: Request, call_next):
        return await decoy_404_middleware(request, call_next, rng=rng)

    @app.middleware("http")
    async def _session(request: Request, call_next):
        request.state.user = user
        request.state.session_token = None
        request.state.clear_session_cookie = False
        return await call_next(request)

    @app.get("/")
    async def _root():
        return PlainTextResponse("root")

    @app.get("/events/{event_path:path}/images/{filename:path}")
    async def _event_image(event_path: str, filename: str):
        # Simulate the real image route: missing images 404.
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="image not found")

    @app.get("/events/{event_path:path}/")
    async def _event(event_path: str):
        # Simulate an event route that 404s for unknown events.
        raise_404 = event_path != "real"
        if raise_404:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="event not found")
        return PlainTextResponse("event")

    return app


async def _get(app, path):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=True,
    ) as client:
        return await client.get(path)


# ---------------------------------------------------------------------------
# End-to-end middleware behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anonymous_bogus_path_returns_plain_decoy():
    """Anonymous bogus 404 -> HTTP 200 with the exact decoy message (90% branch)."""
    app = _build_app(user=None, rng=_ForcePlain())
    resp = await _get(app, "/totally-bogus-path-xyz")

    assert resp.status_code == 200
    assert resp.content == DECOY_MESSAGE_BYTES
    assert resp.text == DECOY_MESSAGE


@pytest.mark.asyncio
async def test_anonymous_bogus_path_returns_gzip_bomb():
    """Anonymous bogus 404 -> gzip bomb (10% branch) that decompresses large."""
    app = _build_app(user=None, rng=_ForceBomb())
    resp = await _get(app, "/totally-bogus-path-xyz")

    assert resp.status_code == 200
    # httpx auto-decodes gzip; verify by decompressing raw bytes ourselves via
    # a fresh response object instead of trusting the client to expose them.
    # Build the response directly to inspect the compressed bytes on the wire.
    built = build_decoy_response(rng=_ForceBomb())
    assert built.headers["Content-Encoding"] == "gzip"
    compressed = built.body
    # Compressed payload on the wire stays small.
    assert len(compressed) < 1 * 1024 * 1024
    decompressed = gzip.decompress(compressed)
    assert len(decompressed) == BOMB_DECOMPRESSED_SIZE
    assert decompressed == b"\0" * BOMB_DECOMPRESSED_SIZE


@pytest.mark.asyncio
async def test_logged_in_user_gets_real_404():
    """A logged-in user hitting a bogus path gets the real 404, not the decoy."""

    class _User:
        email = "a@b.com"

    app = _build_app(user=_User(), rng=_ForcePlain())
    resp = await _get(app, "/totally-bogus-path-xyz")

    assert resp.status_code == 404
    assert resp.content != DECOY_MESSAGE_BYTES


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/robots.txt", "/.well-known/security.txt"])
async def test_standard_web_files_get_real_404(path):
    """/robots.txt and /.well-known/* get the real 404, not the decoy."""
    app = _build_app(user=None, rng=_ForcePlain())
    resp = await _get(app, path)

    assert resp.status_code == 404
    assert resp.content != DECOY_MESSAGE_BYTES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/something.png",
        "/events/2026/nxrs/images/does-not-exist.jpg",
    ],
)
async def test_wellformed_image_paths_get_real_404(path):
    """Well-formed-but-missing image paths get the real 404, not the decoy."""
    app = _build_app(user=None, rng=_ForcePlain())
    resp = await _get(app, path)

    assert resp.status_code == 404
    assert resp.content != DECOY_MESSAGE_BYTES


@pytest.mark.asyncio
async def test_valid_shaped_event_path_gets_real_404():
    """A valid-shaped but missing event path gets the real 404, not the decoy."""
    app = _build_app(user=None, rng=_ForcePlain())
    resp = await _get(app, "/events/nope/nope/")

    assert resp.status_code == 404
    assert resp.content != DECOY_MESSAGE_BYTES


# ---------------------------------------------------------------------------
# Direct unit tests: is_exempt_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/robots.txt",
        "/favicon.ico",
        "/sitemap.xml",
        "/.well-known/security.txt",
        "/.well-known/acme-challenge/abc",
        "/logo.png",
        "/photo.JPG",  # case-insensitive
        "/a/b/c.webp",
        "/events/2026/nxrs/images/x",  # contains /images/
        "/",
        "/events/anything/here/",
        "/login",
        "/login/reset",
        "/logout",
        "/register",
        "/lost-rockets",
        "/static",
        "/static/js/app.js",
    ],
)
def test_is_exempt_path_true(path):
    assert is_exempt_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/totally-bogus-path-xyz",
        "/wp-admin",
        "/wp-login.php",
        "/.env",
        "/config.json",
        "/api/v1/users",
        "/loginx",  # not a real prefix match
        "/adminfoo",
        "/admin",  # no top-level /admin route; must NOT be exempt
        "/admin/users",  # admin lives under /events/.../api/admin, not here
        "",
    ],
)
def test_is_exempt_path_false(path):
    assert is_exempt_path(path) is False


# ---------------------------------------------------------------------------
# Direct unit tests: build_decoy_response
# ---------------------------------------------------------------------------


def test_build_decoy_response_plain_branch():
    resp = build_decoy_response(rng=_ForcePlain())
    assert resp.status_code == 200
    assert resp.body == DECOY_MESSAGE_BYTES
    assert "Content-Encoding" not in resp.headers


def test_build_decoy_response_bomb_branch():
    resp = build_decoy_response(rng=_ForceBomb())
    assert resp.status_code == 200
    assert resp.headers["Content-Encoding"] == "gzip"
    assert len(resp.body) < 1 * 1024 * 1024  # small on the wire
    assert len(gzip.decompress(resp.body)) == BOMB_DECOMPRESSED_SIZE


# ---------------------------------------------------------------------------
# Probabilistic sanity test (loose bounds; not flaky)
# ---------------------------------------------------------------------------


def test_bomb_ratio_is_roughly_ten_percent():
    """Over many draws with a real RNG, ~10% should be bombs (loose bounds)."""
    rng = random.Random(12345)
    draws = 1000
    bombs = 0
    for _ in range(draws):
        resp = build_decoy_response(rng=rng)
        if resp.headers.get("Content-Encoding") == "gzip":
            bombs += 1

    fraction = bombs / draws
    # Loose bounds around BOMB_PROBABILITY (0.10) to document intent without flakiness.
    assert 0.03 <= fraction <= 0.20, f"bomb fraction {fraction} outside sanity bounds"
