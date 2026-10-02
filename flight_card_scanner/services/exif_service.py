"""EXIF metadata extraction service.

Provides helpers to read GPS coordinates from an uploaded image's EXIF
metadata using Pillow. Used by the Found Rockets feature to default the
location of a found rocket to the coordinates embedded in the photo (the
finder can still enter or adjust the values afterwards).

All functions are defensive: any problem reading or parsing metadata results
in a ``None`` coordinate rather than an exception, since EXIF data is
frequently absent, partial, or malformed.
"""

from __future__ import annotations

import io
import logging
from typing import NamedTuple

logger = logging.getLogger(__name__)


class GpsCoordinates(NamedTuple):
    """Decimal-degree GPS coordinates extracted from image metadata."""

    latitude: float
    longitude: float


def _ratio_to_float(value) -> float:
    """Convert an EXIF rational (or plain number) to a float.

    Pillow may return IFDRational objects, tuples of (num, den), or plain
    ints/floats depending on the image and Pillow version.
    """
    # Tuple form: (numerator, denominator)
    if isinstance(value, tuple) and len(value) == 2:
        num, den = value
        return float(num) / float(den) if den else 0.0
    # IFDRational and plain numbers both support float()
    return float(value)


def _dms_to_decimal(dms, ref: str | None) -> float | None:
    """Convert a (degrees, minutes, seconds) EXIF tuple + hemisphere ref to decimal degrees.

    Args:
        dms: A 3-element sequence of degrees, minutes, seconds (each may be an
            EXIF rational).
        ref: Hemisphere reference — one of "N", "S", "E", "W".

    Returns:
        Decimal degrees, negative for southern/western hemispheres, or ``None``
        if the input cannot be parsed.
    """
    try:
        if dms is None or len(dms) != 3:
            return None
        degrees = _ratio_to_float(dms[0])
        minutes = _ratio_to_float(dms[1])
        seconds = _ratio_to_float(dms[2])
    except (TypeError, ValueError, ZeroDivisionError):
        return None

    decimal = degrees + minutes / 60.0 + seconds / 3600.0

    if ref and ref.upper() in ("S", "W"):
        decimal = -decimal

    # Reject obviously invalid coordinates.
    return decimal


def extract_gps_coordinates(image_bytes: bytes) -> GpsCoordinates | None:
    """Extract GPS latitude/longitude (decimal degrees) from image EXIF metadata.

    Args:
        image_bytes: Raw bytes of a JPEG/PNG (or any Pillow-readable) image.

    Returns:
        A ``GpsCoordinates`` namedtuple if valid GPS data is present, otherwise
        ``None``. Never raises for missing/malformed metadata.
    """
    try:
        from PIL import Image
        from PIL.ExifTags import GPSTAGS, TAGS
    except Exception as exc:  # pragma: no cover - Pillow should be installed
        logger.warning("Pillow not available for EXIF extraction: %s", exc)
        return None

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            exif = img.getexif()
            if not exif:
                return None

            # Find the GPS IFD. Prefer the dedicated accessor when available.
            gps_info = None
            try:
                from PIL.ExifTags import IFD

                gps_info = exif.get_ifd(IFD.GPSInfo)
            except Exception:
                gps_info = None

            if not gps_info:
                # Fall back to scanning top-level tags for GPSInfo.
                for tag_id, value in exif.items():
                    if TAGS.get(tag_id) == "GPSInfo" and isinstance(value, dict):
                        gps_info = value
                        break

            if not gps_info:
                return None

            # Map numeric GPS tag ids to their names.
            gps = {GPSTAGS.get(k, k): v for k, v in gps_info.items()}

            lat = _dms_to_decimal(
                gps.get("GPSLatitude"), gps.get("GPSLatitudeRef")
            )
            lon = _dms_to_decimal(
                gps.get("GPSLongitude"), gps.get("GPSLongitudeRef")
            )

            if lat is None or lon is None:
                return None

            # Sanity-check ranges.
            if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
                logger.debug("Discarding out-of-range EXIF GPS: %s, %s", lat, lon)
                return None

            return GpsCoordinates(latitude=lat, longitude=lon)
    except Exception as exc:
        logger.debug("Failed to read EXIF GPS data: %s", exc)
        return None
