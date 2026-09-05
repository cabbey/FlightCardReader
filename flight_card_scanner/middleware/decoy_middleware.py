"""Defensive decoy middleware for hostile 404s.

When a request would result in a 404 and the path is clearly not something
that could ever have been valid (see ``is_exempt_path``), we do not return an
honest 404. Instead we return a decoy response that looks like a normal 200 so
the attacker/scanner gets no signal that the path was invalid.

The decoy body is a fixed message. 10% of the time we instead return a
compression bomb: a tiny compressed payload that decompresses to a large,
highly compressible blob. This is only dangerous to a client that blindly
auto-decompresses the response; it costs the server almost nothing to serve.

Bomb source
-----------
Rather than compute our own bomb, we reuse David Fifield's well-known "better
zip bomb" archives from https://www.bamsoftware.com/hacks/zipbomb/ . They are
vendored into ``flight_card_scanner/static/zipbombs/`` (see the ``about.txt``
there and ``scripts/fetch_zipbombs.py``) so the decoy path only ever reads local
files -- it never depends on a live external fetch, and cannot amplify hostile
traffic into a per-request download.

Encoding selection ("depending on what type of compression the requestor asks
for")
-----------------------------------------------------------------------------
An HTTP client advertises which compressions it will transparently decode via
the ``Accept-Encoding`` request header, and the server signals what it actually
used via the ``Content-Encoding`` response header. We honor the requester's
advertised encoding:

* ``gzip``    -> we serve the bamsoftware archive's raw DEFLATE *kernel*
  re-framed as a gzip stream (``Content-Encoding: gzip``). A client that
  auto-decompresses gzip inflates it into a huge payload.
* ``deflate`` -> the same kernel re-framed as a zlib/DEFLATE stream
  (``Content-Encoding: deflate``).
* neither (identity only) -> a client that accepts no decompressible encoding
  will not auto-inflate a gzip/deflate body, so we instead hand it the raw
  ``.zip`` archive as a download (``Content-Type: application/zip``). It only
  detonates if the client actually unpacks the archive.

Note on ratios: a single HTTP ``Content-Encoding`` stream is one DEFLATE stream,
whose ratio is capped near 1032:1, so the gzip/deflate responses expand to the
kernel's single-file size (tens of MB), not the archive's full multi-file total.
The multi-terabyte expansion is a property of the ZIP *container* (many files
overlapping one kernel) and is only realized when a client unpacks the ``.zip``
we hand to identity-only clients. Either way the bytes on the wire stay tiny and
the server-side cost is a constant, precomputed buffer copy.

Design notes / rationale:
- We return HTTP 200 (not 404) for the decoy so a scanner cannot distinguish a
  "real" missing path from a decoyed one. Revealing 404 would defeat the point.
  (This matches the defensive pattern described at
  https://idiallo.com/blog/zipbomb-protection .)
- The decision logic (``is_exempt_path`` / ``select_bomb_encoding`` /
  ``build_decoy_response``) lives in standalone, pure functions so it can be
  unit-tested without booting the app.
- The RNG is injectable so tests can force each branch deterministically.
- All served bomb payloads are precomputed once at import, so building a bomb
  response is essentially free per request even under a hostile scanning burst.
"""

from __future__ import annotations

import struct
import zlib
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

# Probability that a decoy response is a bomb rather than the plain message.
# 0.10 => ~10% bombs, ~90% plain messages.
BOMB_PROBABILITY = 0.10

# Directory holding the vendored bamsoftware zip bombs (see about.txt there).
_ZIPBOMB_DIR = Path(__file__).resolve().parent.parent / "static" / "zipbombs"

# Which vendored archive to use as the bomb source. zbsm.zip (42 kB -> 5.5 GB)
# is small on disk and its single DEFLATE kernel already expands to ~21 MB per
# HTTP stream -- more than enough to wreck a client that auto-decompresses,
# while keeping repo size and the vendored payload tiny.
_BOMB_ARCHIVE = "zbsm.zip"


# ---------------------------------------------------------------------------
# Load / precompute the bomb payloads ONCE at import time.
# ---------------------------------------------------------------------------


def _extract_first_deflate_kernel(zip_path: Path) -> bytes:
    """Return the raw DEFLATE bytes of the first entry in ``zip_path``.

    The bamsoftware bombs are ZIP archives whose entries share one DEFLATE
    "kernel". We read the raw compressed bytes of the first entry directly from
    the local file header (we do NOT decompress it here) so we can re-frame that
    single DEFLATE stream as a gzip/deflate HTTP body. We parse the ZIP local
    file header by hand rather than using ``zipfile.ZipFile.open`` because
    modern Python raises ``BadZipFile('Overlapped entries')`` on exactly this
    (intentionally) overlapping construction.
    """
    import zipfile

    zf = zipfile.ZipFile(zip_path)
    info = zf.infolist()[0]
    with open(zip_path, "rb") as fh:
        fh.seek(info.header_offset)
        local_header = fh.read(30)
        # Offsets 26..28 = filename length, 28..30 = extra-field length.
        name_len = struct.unpack("<H", local_header[26:28])[0]
        extra_len = struct.unpack("<H", local_header[28:30])[0]
        fh.seek(info.header_offset + 30 + name_len + extra_len)
        return fh.read(info.compress_size)


def _gzip_wrap(raw_deflate: bytes, decompressed: bytes) -> bytes:
    """Wrap a raw DEFLATE stream in a gzip container.

    gzip = 10-byte header + raw DEFLATE + 8-byte trailer (CRC-32 and ISIZE of
    the *decompressed* data, little-endian). ``decompressed`` is required to
    compute the trailer; we do this once at import, never per request.
    """
    header = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff"
    crc = zlib.crc32(decompressed) & 0xFFFFFFFF
    isize = len(decompressed) & 0xFFFFFFFF
    return header + raw_deflate + struct.pack("<II", crc, isize)


def _zlib_wrap(raw_deflate: bytes, decompressed: bytes) -> bytes:
    """Wrap a raw DEFLATE stream in a zlib container (for Content-Encoding: deflate).

    zlib = 2-byte header (0x78 0x9c) + raw DEFLATE + 4-byte big-endian Adler-32
    of the decompressed data. RFC 7230 says ``deflate`` means the zlib format;
    serving the zlib-wrapped stream is the most spec-correct choice.
    """
    adler = zlib.adler32(decompressed) & 0xFFFFFFFF
    return b"\x78\x9c" + raw_deflate + struct.pack(">I", adler)


def _load_bomb_payloads() -> tuple[bytes, bytes, bytes, int]:
    """Precompute (gzip_body, deflate_body, zip_archive_body, decompressed_size).

    Done once at import. Decompressing the kernel here (to compute the gzip CRC
    and deflate Adler-32) touches the kernel's single-file expansion (~21 MB for
    zbsm.zip), which is cheap and happens exactly once at startup -- never on the
    hostile request path.
    """
    archive_path = _ZIPBOMB_DIR / _BOMB_ARCHIVE
    zip_archive_body = archive_path.read_bytes()
    raw_deflate = _extract_first_deflate_kernel(archive_path)
    decompressed = zlib.decompressobj(-zlib.MAX_WBITS).decompress(raw_deflate)
    decompressed_size = len(decompressed)
    gzip_body = _gzip_wrap(raw_deflate, decompressed)
    deflate_body = _zlib_wrap(raw_deflate, decompressed)
    return gzip_body, deflate_body, zip_archive_body, decompressed_size


(
    _BOMB_GZIP_BYTES,
    _BOMB_DEFLATE_BYTES,
    _BOMB_ZIP_BYTES,
    BOMB_DECOMPRESSED_SIZE,
) = _load_bomb_payloads()


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


def select_bomb_encoding(accept_encoding: str | None) -> str:
    """Pick the bomb response encoding from the requester's ``Accept-Encoding``.

    Returns one of ``"gzip"``, ``"deflate"``, or ``"zip"``:

    * ``"gzip"`` / ``"deflate"`` when the client advertised that it will
      transparently decode that HTTP content-encoding (we then serve a matching
      ``Content-Encoding`` stream the client auto-inflates). ``gzip`` is
      preferred when both are offered because it is the most universally
      auto-decompressed.
    * ``"zip"`` when the client accepts no decompressible encoding (identity
      only, or a header we do not serve like ``br``). Such a client will not
      auto-inflate a gzip/deflate body, so we hand it the raw ``.zip`` archive
      as a download instead.

    This is a pure function of the header string so it is trivially unit-tested.
    We ignore q-values / weighting: a scanner that lists an encoding at all is
    signalling it can decode it, which is all we need.
    """
    if not accept_encoding:
        return "zip"
    tokens = {tok.strip().lower() for tok in accept_encoding.split(",")}
    # Strip any ";q=..." weighting from each token.
    codings = {tok.split(";", 1)[0].strip() for tok in tokens}
    if "gzip" in codings:
        return "gzip"
    if "deflate" in codings:
        return "deflate"
    return "zip"


def _build_bomb_response(accept_encoding: str | None) -> Response:
    """Build a compression-bomb decoy response matching the requested encoding.

    All payloads are precomputed at import (see ``_load_bomb_payloads``), so this
    is just selecting a constant buffer and framing headers -- essentially free
    on the server side and unable to amplify hostile traffic into memory/CPU
    pressure. We do NOT set an explicit Content-Length; Starlette derives it from
    the actual (tiny, compressed) bytes so it can never contradict the payload.

    NOTE: the gzip/deflate variants are only harmful to a client that
    auto-decompresses the body; the ``.zip`` variant only to a client that
    unpacks the archive.
    """
    encoding = select_bomb_encoding(accept_encoding)
    if encoding == "gzip":
        return Response(
            content=_BOMB_GZIP_BYTES,
            status_code=200,
            media_type="text/plain",
            headers={"Content-Encoding": "gzip"},
        )
    if encoding == "deflate":
        return Response(
            content=_BOMB_DEFLATE_BYTES,
            status_code=200,
            media_type="text/plain",
            headers={"Content-Encoding": "deflate"},
        )
    # Identity-only client: serve the raw zip archive as a download.
    return Response(
        content=_BOMB_ZIP_BYTES,
        status_code=200,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="download.zip"'},
    )


def _build_plain_decoy_response() -> Response:
    """Build the plain-text decoy response with the exact decoy message."""
    return Response(
        content=DECOY_MESSAGE_BYTES,
        status_code=200,
        media_type="text/plain",
    )


def build_decoy_response(
    rng: random.Random | None = None,
    accept_encoding: str | None = None,
) -> Response:
    """Return a decoy response: bomb 10% of the time, plain message 90%.

    ``rng`` is an injectable ``random.Random`` (or compatible) so tests can
    force a specific branch deterministically. When omitted, the module-level
    ``random`` module is used.

    ``accept_encoding`` is the requester's ``Accept-Encoding`` header value; the
    bomb branch uses it to pick the response encoding (see
    ``select_bomb_encoding``). When omitted (no header), the bomb is served as a
    downloadable ``.zip`` archive.

    The decoy always uses HTTP 200 so it is indistinguishable from a normal
    successful response (the whole point is to hide that this was a 404).
    """
    source = rng if rng is not None else random
    if source.random() < BOMB_PROBABILITY:
        return _build_bomb_response(accept_encoding)
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
    plain/bomb branch deterministically while still exercising this real
    entrypoint end-to-end. Production callers omit it and get real randomness.
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

    # Clearly-bogus 404 for an anonymous user: return the decoy, matching the
    # compression the requester said it can decode.
    return build_decoy_response(
        rng=rng,
        accept_encoding=request.headers.get("accept-encoding"),
    )
