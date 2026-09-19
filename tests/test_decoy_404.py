"""Tests for the defensive decoy-404 middleware.

App construction, session mocking, and AsyncClient usage are modeled on
tests/test_multi_event_routing.py.

The image decoy is served from the vendored unicorn images (see
flight_card_scanner/static/unicorns/about.txt), so these tests assert on that
image behavior (an image/* Content-Type and valid image bytes).
"""

from __future__ import annotations

import random

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from httpx import ASGITransport, AsyncClient

from flight_card_scanner.middleware.decoy_middleware import (
    DECOY_MESSAGE,
    DECOY_MESSAGE_BYTES,
    IMAGE_PROBABILITY,
    build_decoy_response,
    decoy_404_middleware,
    is_exempt_path,
)

# Magic-number prefixes for the image formats we vendor, so tests can confirm
# the decoy really returned valid image bytes.
_IMAGE_MAGIC = {
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/jpeg": b"\xff\xd8\xff",
}


# ---------------------------------------------------------------------------
# Deterministic RNGs for forcing each branch
# ---------------------------------------------------------------------------


class _ForceImage:
    """RNG whose random() always returns 0.0 (< IMAGE_PROBABILITY => image).

    ``choice`` returns the first item so the chosen image is deterministic.
    """

    def random(self) -> float:
        return 0.0

    def choice(self, seq):
        return seq[0]


class _ForcePlain:
    """RNG whose random() always returns 0.99 (>= IMAGE_PROBABILITY => plain)."""

    def random(self) -> float:
        return 0.99

    def choice(self, seq):
        return seq[0]


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
    # plain/image branch deterministically.
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


async def _get(app, path, headers=None):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=True,
    ) as client:
        return await client.get(path, headers=headers)


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
async def test_anonymous_bogus_path_returns_unicorn_image():
    """Anonymous bogus 404 -> a unicorn image (10% branch).

    The response is HTTP 200 with an image/* Content-Type and valid image bytes
    (verified via the format magic number).
    """
    app = _build_app(user=None, rng=_ForceImage())
    resp = await _get(app, "/totally-bogus-path-xyz")

    assert resp.status_code == 200
    content_type = resp.headers["content-type"]
    assert content_type.startswith("image/")
    assert content_type in _IMAGE_MAGIC
    assert resp.content.startswith(_IMAGE_MAGIC[content_type])
    # Small on the wire.
    assert 0 < len(resp.content) < 1 * 1024 * 1024
    # It is not the plain-text message.
    assert resp.content != DECOY_MESSAGE_BYTES


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
    assert resp.media_type == "text/plain"


def test_build_decoy_response_image_branch():
    resp = build_decoy_response(rng=_ForceImage())
    assert resp.status_code == 200
    assert resp.media_type in _IMAGE_MAGIC
    assert resp.body.startswith(_IMAGE_MAGIC[resp.media_type])
    assert 0 < len(resp.body) < 1 * 1024 * 1024  # small on the wire


def test_build_decoy_response_image_choices_are_all_valid():
    """Every vendored image the RNG can pick is a valid image with a real magic."""
    from flight_card_scanner.middleware.decoy_middleware import _UNICORN_IMAGES

    assert len(_UNICORN_IMAGES) >= 2  # a few distinct images are vendored
    for body, media_type in _UNICORN_IMAGES:
        assert media_type in _IMAGE_MAGIC
        assert body.startswith(_IMAGE_MAGIC[media_type])

    # At least one PNG and one JPEG so multiple content types are exercised.
    media_types = {mt for _, mt in _UNICORN_IMAGES}
    assert "image/png" in media_types
    assert "image/jpeg" in media_types


# ---------------------------------------------------------------------------
# Probabilistic sanity test (loose bounds; not flaky)
# ---------------------------------------------------------------------------


def test_image_ratio_is_roughly_ten_percent():
    """Over many draws with a real RNG, ~10% should be images (loose bounds)."""
    rng = random.Random(12345)
    draws = 1000
    images = 0
    for _ in range(draws):
        resp = build_decoy_response(rng=rng)
        if (resp.media_type or "").startswith("image/"):
            images += 1

    fraction = images / draws
    # Loose bounds around IMAGE_PROBABILITY (0.10) to document intent without flakiness.
    assert 0.03 <= fraction <= 0.20, f"image fraction {fraction} outside sanity bounds"
