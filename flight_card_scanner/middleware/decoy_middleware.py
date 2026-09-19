"""Defensive decoy middleware for hostile 404s.

When a request would result in a 404 and the path is clearly not something
that could ever have been valid (see ``is_exempt_path``), we do not return an
honest 404. Instead we return a decoy response that looks like a normal 200 so
the attacker/scanner gets no signal that the path was invalid.

The decoy body is a fixed message. ~10% of the time we instead return a picture
of a unicorn: a small, harmless image chosen at random from a vendored set. This
costs the server almost nothing to serve and simply gives a scanner a
non-informative 200.

Image source
------------
Rather than fetch anything on the request path, we vendor a handful of small
unicorn images into ``flight_card_scanner/static/unicorns/`` (see the
``about.txt`` there and ``scripts/make_unicorns.py``) so the decoy path only ever
reads local files -- it never depends on a live external fetch, and cannot
amplify hostile traffic into a per-request download. The images are original
CC0 artwork.

Design notes / rationale:
- We return HTTP 200 (not 404) for the decoy so a scanner cannot distinguish a
  "real" missing path from a decoyed one. Revealing 404 would defeat the point.
  (This matches the defensive pattern described at
  https://idiallo.com/blog/zipbomb-protection .)
- The decision logic (``is_exempt_path`` / ``build_decoy_response``) lives in
  standalone, pure functions so it can be unit-tested without booting the app.
- The RNG is injectable so tests can force each branch (and each image choice)
  deterministically.
- All served image payloads are read once at import, so building an image decoy
  response is essentially free per request even under a hostile scanning burst.
"""

from __future__ import annotations

from pathlib import Path

from starlette.requests import Request
from starlette.responses import Response

import random

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Exact decoy message. Do NOT change punctuation/casing/whitespace: tests and
# the product requirement assert on these exact bytes.
DECOY_MESSAGE = "these are not the droids you are looking for"
DECOY_MESSAGE_BYTES = DECOY_MESSAGE.encode("utf-8")

# Probability that a decoy response is a unicorn image rather than the plain
# message. 0.10 => ~10% images, ~90% plain messages.
IMAGE_PROBABILITY = 0.10

# Directory holding the vendored unicorn images (see about.txt there).
_UNICORN_DIR = Path(__file__).resolve().parent.parent / "static" / "unicorns"

# Map file extensions to the Content-Type we serve them with.
_IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


# ---------------------------------------------------------------------------
# Load the unicorn image payloads ONCE at import time.
# ---------------------------------------------------------------------------


def _load_unicorn_images() -> tuple[tuple[bytes, str], ...]:
    """Read every vendored unicorn image into (bytes, media_type) pairs.

    Done once at import so serving an image decoy is just picking a precomputed
    (bytes, media_type) pair and copying the buffer -- essentially free on the
    request path even under a hostile scanning burst. Files whose extension we
    do not recognize are skipped (e.g. the about.txt).
    """
    images: list[tuple[bytes, str]] = []
    for path in sorted(_UNICORN_DIR.iterdir()):
        if not path.is_file():
            continue
        media_type = _IMAGE_MEDIA_TYPES.get(path.suffix.lower())
        if media_type is None:
            continue
        images.append((path.read_bytes(), media_type))
    if not images:
        raise RuntimeError(
            f"no vendored unicorn images found in {_UNICORN_DIR}; "
            "run scripts/make_unicorns.py"
        )
    return tuple(images)


_UNICORN_IMAGES = _load_unicorn_images()


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
# NOTE: there is deliberately NO "/admin" here. Admin endpoints live under
# "/events/{event_path}/api/admin/..." (already covered by the "/events/"
# exemption), not at a top-level "/admin". Exempting a bare "/admin" would hand
# a common scanner probe an honest 404 instead of the decoy, weakening the
# defense on exactly the kind of reconnaissance path this feature targets.
_KNOWN_TOP_LEVEL_ROUTES = (
    "/login",
    "/logout",
    "/register",
    "/lost-rockets",
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


def _build_image_decoy_response(rng: random.Random) -> Response:
    """Build a unicorn-image decoy response.

    Picks one of the vendored images at random (via the injectable ``rng`` so
    tests are deterministic) and serves its precomputed bytes with the matching
    image Content-Type. This is just selecting a constant buffer -- essentially
    free on the server side and unable to amplify hostile traffic. We do NOT set
    an explicit Content-Length; Starlette derives it from the actual image
    bytes so it can never contradict the payload.
    """
    body, media_type = rng.choice(_UNICORN_IMAGES)
    return Response(
        content=body,
        status_code=200,
        media_type=media_type,
    )


def _build_plain_decoy_response() -> Response:
    """Build the plain-text decoy response with the exact decoy message."""
    return Response(
        content=DECOY_MESSAGE_BYTES,
        status_code=200,
        media_type="text/plain",
    )


def build_decoy_response(rng: random.Random | None = None) -> Response:
    """Return a decoy response: unicorn image ~10% of the time, plain message ~90%.

    ``rng`` is an injectable ``random.Random`` (or compatible) so tests can
    force a specific branch -- and a specific image -- deterministically. When
    omitted, the module-level ``random`` module is used.

    The decoy always uses HTTP 200 so it is indistinguishable from a normal
    successful response (the whole point is to hide that this was a 404).
    """
    source = rng if rng is not None else random
    if source.random() < IMAGE_PROBABILITY:
        return _build_image_decoy_response(source)
    return _build_plain_decoy_response()


# ---------------------------------------------------------------------------
# Middleware entry point
# ---------------------------------------------------------------------------


async def decoy_404_middleware(
    request: Request, call_next, rng: random.Random | None = None
):
    """HTTP middleware that replaces hostile 404s with a decoy response.

    This must run AFTER session resolution so ``request.state.user`` is
    populated. In Starlette the last-added ``@app.middleware('http')`` runs
    outermost, so register/define this middleware BEFORE ``session_resolution``
    in ``main.py`` (making session_resolution the outer wrapper that runs
    first).

    ``rng`` is an optional injectable ``random.Random`` (or compatible) that is
    threaded through to ``build_decoy_response`` so tests can force the
    plain/image branch (and the chosen image) deterministically while still
    exercising this real entrypoint end-to-end. Production callers omit it and
    get real randomness.
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
    return build_decoy_response(rng=rng)
