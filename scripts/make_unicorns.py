#!/usr/bin/env python3.12
"""Generate the vendored unicorn images used by the decoy-404 middleware.

The decoy-404 middleware (``flight_card_scanner/middleware/decoy_middleware.py``)
serves a picture of a unicorn to hostile, anonymous scanners that hit
clearly-bogus 404 paths, ~10% of the time (the other ~90% get a fixed plaintext
message). Rather than depend on a live external fetch on the request path, we
vendor a small set of unicorn images into
``flight_card_scanner/static/unicorns/`` and the middleware only ever reads
those local files (at import time).

Provenance / licensing: these images are ORIGINAL artwork drawn by this script
using Pillow. Because they are our own creation, we release them into the public
domain (Creative Commons CC0 1.0). There is no third-party copyright to track.
See ``flight_card_scanner/static/unicorns/about.txt``.

Run this script to (re)generate the assets; the generated files are committed to
the repo.

Usage:
    python3.12 scripts/make_unicorns.py
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageDraw

_DEST_DIR = (
    Path(__file__).resolve().parent.parent
    / "flight_card_scanner"
    / "static"
    / "unicorns"
)

# Canvas size. Kept small so the vendored assets and per-request buffers stay
# tiny (a few KB each).
_SIZE = 256


def _draw_unicorn(
    body: tuple[int, int, int],
    mane: tuple[int, int, int],
    horn: tuple[int, int, int],
    bg: tuple[int, int, int],
) -> Image.Image:
    """Draw a simple, friendly cartoon unicorn head on a solid background.

    Deliberately minimal vector-ish shapes: a rounded head, an ear, a spiral-ish
    horn, a flowing mane, an eye, and a nostril. Different color palettes give us
    a handful of visually distinct unicorns from one routine.
    """
    img = Image.new("RGB", (_SIZE, _SIZE), bg)
    d = ImageDraw.Draw(img)

    # Mane (drawn first so the head overlaps it).
    d.ellipse([28, 70, 120, 210], fill=mane)
    d.ellipse([40, 40, 120, 130], fill=mane)

    # Head: a rounded muzzle shape.
    d.ellipse([80, 70, 210, 200], fill=body)
    d.polygon([(120, 150), (95, 205), (165, 200)], fill=body)  # muzzle taper

    # Ear.
    d.polygon([(150, 62), (168, 30), (182, 70)], fill=body)

    # Horn (stacked triangles to suggest a spiral).
    d.polygon([(120, 78), (150, 78), (135, 18)], fill=horn)
    d.line([(128, 62), (142, 62)], fill=bg, width=3)
    d.line([(130, 48), (140, 48)], fill=bg, width=3)

    # Eye.
    d.ellipse([150, 105, 172, 130], fill=(30, 30, 40))
    d.ellipse([156, 110, 164, 118], fill=(255, 255, 255))

    # Nostril.
    d.ellipse([120, 178, 132, 190], fill=(60, 40, 50))

    return img


# (filename, body, mane, horn, background) palettes -> a few distinct unicorns.
_UNICORNS: tuple[tuple[str, tuple, tuple, tuple, tuple], ...] = (
    (
        "unicorn_classic.png",
        (250, 246, 250),  # white body
        (255, 145, 190),  # pink mane
        (255, 210, 90),  # gold horn
        (198, 232, 255),  # sky blue bg
    ),
    (
        "unicorn_lavender.png",
        (226, 214, 250),  # lavender body
        (150, 120, 230),  # purple mane
        (255, 235, 120),  # pale gold horn
        (245, 235, 255),  # very light lavender bg
    ),
    (
        "unicorn_mint.png",
        (222, 250, 235),  # mint body
        (90, 200, 170),  # teal mane
        (255, 200, 100),  # amber horn
        (255, 245, 230),  # cream bg
    ),
)

# One image is emitted as JPEG so the decoy exercises more than one content type.
_JPEG_FILES = {"unicorn_mint.png": "unicorn_sunset.jpg"}


def main() -> int:
    _DEST_DIR.mkdir(parents=True, exist_ok=True)
    for name, body, mane, horn, bg in _UNICORNS:
        img = _draw_unicorn(body, mane, horn, bg)
        dest = _DEST_DIR / name
        img.save(dest, format="PNG", optimize=True)
        data = dest.read_bytes()
        print(f"wrote {dest} ({len(data)} bytes) sha256={hashlib.sha256(data).hexdigest()}")

        jpeg_name = _JPEG_FILES.get(name)
        if jpeg_name:
            jdest = _DEST_DIR / jpeg_name
            img.save(jdest, format="JPEG", quality=85)
            jdata = jdest.read_bytes()
            print(
                f"wrote {jdest} ({len(jdata)} bytes) "
                f"sha256={hashlib.sha256(jdata).hexdigest()}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
