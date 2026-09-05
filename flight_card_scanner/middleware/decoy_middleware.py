"""Defensive decoy middleware for hostile 404s.

When a request would result in a 404 and the path is clearly not something
that could ever have been valid (see ``is_exempt_path``), we do not return an
honest 404. Instead we return a decoy response that looks like a normal 200 so
the attacker/scanner gets no signal that the path was invalid.

The decoy body is a fixed message. 10% of the time we instead return a gzip
bomb: a tiny compressed payload that decompresses to a large, highly
compressible blob. This is only dangerous to a client that blindly
auto-decompresses the response; it costs the server almost nothing to build.

Design notes / rationale:
- We return HTTP 200 (not 404) for the decoy so a scanner cannot distinguish a
  "real" missing path from a decoyed one. Revealing 404 would defeat the point.
- The decision logic (``is_exempt_path`` / ``build_decoy_response``) lives in
  standalone, pure functions so it can be unit-tested without booting the app.
- The RNG is injectable so tests can force each branch deterministically.
"""

from __future__ import annotations

import gzip
import random

from starlette.requests import Request
from starlette.responses import Response

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Exact decoy message. Do NOT change punctuation/casing/whitespace: tests and
# the product requirement assert on these exact bytes.
DECOY_MESSAGE = "these are not the droids you are looking for"
DECOY_MESSAGE_BYTES = DECOY_MESSAGE.encode("utf-8")

# Probability that a decoy response is a gzip bomb rather than the plain
# message. 0.10 => ~10% bombs, ~90% plain messages.
BOMB_PROBABILITY = 0.10

# Size (in bytes) of the DECOMPRESSED gzip-bomb payload. Tens of MB is enough
# to be a meaningful bomb for a client that auto-decompresses, while the
# COMPRESSED bytes on the wire stay tiny (a run of a single byte compresses to
# a few KB). We never materialize this full payload as a persistent object
# beyond the single compression call below.
BOMB_DECOMPRESSED_SIZE = 50 * 1024 * 1024  # 50 MiB decompressed

# Common image file extensions. A request for one of these that 404s is
# treated as a "might have been a real image" path and is left as an honest
# 404 rather than decoyed.
_IMAGE_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
)

# Standard web files that well-behaved clients and crawlers request. These get
# an honest 404, never the decoy.
_STANDARD_WEB_FILES = (
    "/robots.txt",
    "/favicon.ico",
    "/sitemap.xml",
)

# Known top-level application route prefixes. A 404 under one of these is a
# legitimately-shaped path (record/user simply may not exist), so it is exempt.
_KNOWN_TOP_LEVEL_ROUTES = (
    "/login",
    "/logout",
    "/register",
    "/lost-rockets",
    "/admin",
    "/static",
)


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


def is_exempt_path(path: str) -> bool:
    """Return True if ``path`` should keep its original 404 (never decoyed).

    A path is exempt when it is either a standard web file or a
    "might have been valid" path. Only clearly-bogus paths get the decoy.
    """
    if not path:
        return False

    lowered = path.lower()

    # --- Standard web files (crawlers / browsers legitimately request these).
    if path in _STANDARD_WEB_FILES:
        return True
    if path.startswith("/.well-known/"):
        return True

    # --- Might-have-been-valid: image paths.
    # A well-formed image request (by extension) or anything under an event
    # image route ("/images/") could plausibly have been a real image that
    # simply does not exist, so we do not decoy it.
    if lowered.endswith(_IMAGE_EXTENSIONS):
        return True
    if "/images/" in path:
        return True

    # --- Might-have-been-valid: valid-shaped event / flight-card paths.
    # "/" is the events list; "/events/..." is the event-scoped namespace where
    # an unknown slug or record legitimately 404s. These are real routes.
    if path == "/":
        return True
    if path.startswith("/events/"):
        return True

    # --- Might-have-been-valid: known top-level app routes.
    for route in _KNOWN_TOP_LEVEL_ROUTES:
        if path == route or path.startswith(route + "/"):
            return True

    return False


def _build_gzip_bomb_response() -> Response:
    """Build a gzip-bomb decoy response.

    The body is a gzip stream that decompresses to ``BOMB_DECOMPRESSED_SIZE``
    bytes of a single repeated byte (highly compressible => tiny on the wire).
    We do NOT set an explicit Content-Length; Starlette derives it from the
    actual compressed bytes so it can never contradict the payload.

    NOTE: this is only harmful to a client that auto-decompresses the body.
    Compressing a run of zeros is cheap and the compressed output is tiny, so
    the server side stays bounded.
    """
    # Compressing a run of a single byte is cheap and yields a tiny output.
    compressed = gzip.compress(b"\0" * BOMB_DECOMPRESSED_SIZE)
    return Response(
        content=compressed,
        status_code=200,
        media_type="text/plain",
        headers={"Content-Encoding": "gzip"},
    )


def _build_plain_decoy_response() -> Response:
    """Build the plain-text decoy response with the exact decoy message."""
    return Response(
        content=DECOY_MESSAGE_BYTES,
        status_code=200,
        media_type="text/plain",
    )


def build_decoy_response(rng: random.Random | None = None) -> Response:
    """Return a decoy response: gzip bomb 10% of the time, plain message 90%.

    ``rng`` is an injectable ``random.Random`` (or compatible) so tests can
    force a specific branch deterministically. When omitted, the module-level
    ``random`` module is used.

    The decoy always uses HTTP 200 so it is indistinguishable from a normal
    successful response (the whole point is to hide that this was a 404).
    """
    source = rng if rng is not None else random
    if source.random() < BOMB_PROBABILITY:
        return _build_gzip_bomb_response()
    return _build_plain_decoy_response()


# ---------------------------------------------------------------------------
# Middleware entry point
# ---------------------------------------------------------------------------


async def decoy_404_middleware(request: Request, call_next):
    """HTTP middleware that replaces hostile 404s with a decoy response.

    This must run AFTER session resolution so ``request.state.user`` is
    populated. In Starlette the last-added ``@app.middleware('http')`` runs
    outermost, so register/define this middleware BEFORE ``session_resolution``
    in ``main.py`` (making session_resolution the outer wrapper that runs
    first).
    """
    response = await call_next(request)

    if response.status_code != 404:
        return response

    # Logged-in users always get the real 404 (real error pages), never a decoy.
    user = getattr(request.state, "user", None)
    if user is not None:
        return response

    # Exempt paths (standard web files / might-have-been-valid) keep the 404.
    if is_exempt_path(request.url.path):
        return response

    # Clearly-bogus 404 for an anonymous user: return the decoy.
    return build_decoy_response()
