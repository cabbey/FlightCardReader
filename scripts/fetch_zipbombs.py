#!/usr/bin/env python3.12
"""Fetch the defensive zip-bomb assets from bamsoftware.com and vendor them.

The decoy-404 middleware (``flight_card_scanner/middleware/decoy_middleware.py``)
serves a compression bomb to hostile, anonymous scanners that hit clearly-bogus
404 paths. Rather than compute our own bomb at runtime, we reuse David Fifield's
well-known "better zip bomb" archives from:

    https://www.bamsoftware.com/hacks/zipbomb/

These are ZIP archives built with the quoted-overlap DEFLATE construction. We
vendor them into ``flight_card_scanner/static/zipbombs/`` so the decoy path never
depends on a live external fetch (the middleware only reads local files at import
time). Run this script once to (re)download the assets; the downloaded files are
committed to the repo.

Provenance / licensing: these files are published by bamsoftware.com as public
demonstration artifacts. We redistribute them unmodified. See
``flight_card_scanner/static/zipbombs/about.txt``.

Usage:
    python3.12 scripts/fetch_zipbombs.py
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

# The bombs we vendor. We deliberately use the two DEFLATE-only (non-Zip64)
# archives, which are the most broadly compatible with naive clients:
#   zbsm.zip  42 kB   -> 5.5 GB
#   zblg.zip  9.9 MB  -> 281 TB
# We skip zbxl.zip (Zip64, 46 MB) to keep the repo small; the two above are more
# than enough to overwhelm any client that blindly auto-decompresses.
_BASE_URL = "https://www.bamsoftware.com/hacks/zipbomb/"
_ASSETS = ("zbsm.zip", "zblg.zip")

_DEST_DIR = (
    Path(__file__).resolve().parent.parent
    / "flight_card_scanner"
    / "static"
    / "zipbombs"
)


def fetch_one(name: str) -> None:
    url = _BASE_URL + name
    dest = _DEST_DIR / name
    print(f"fetching {url} -> {dest}")
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 (trusted host)
        data = resp.read()
    dest.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    print(f"  wrote {len(data)} bytes, sha256={digest}")


def main() -> int:
    _DEST_DIR.mkdir(parents=True, exist_ok=True)
    for name in _ASSETS:
        fetch_one(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
