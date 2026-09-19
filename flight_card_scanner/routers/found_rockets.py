"""Found Rockets router: report and list rockets found in the field.

Found rockets work outside the construct of a single launch event — a flier who
finds a rocket uploads a photo, and GPS coordinates are extracted from the
image metadata to default the location (editable afterwards). The finder can
add a description and mark the rocket as still in the field or recovered.

Uploaded images require admin approval before they are visible to anyone; the
image URL embeds an unguessable token so it cannot be guessed before approval.
The poster or an admin can mark a found rocket as reunited with its flier,
which removes it from the public listing.

Routes:
- GET  /found-rockets                     -- public listing of found rockets
- GET  /found-rockets/report              -- report form (FLYER)
- POST /found-rockets/api/extract-gps     -- extract EXIF GPS from an image (FLYER)
- POST /found-rockets/api/report          -- submit a found rocket (FLYER)
- POST /found-rockets/api/{found_id}/reunite -- mark reunited (poster or admin)
- GET  /found-rockets/images/{filename}   -- serve an image (approved, or admin)
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..dependencies.auth import Role, require_role
from ..found_rockets_database import get_found_rockets_db
from ..found_rockets_models import (
    VALID_STATUSES,
    FoundRocket,
    STATUS_RECOVERED,
    STATUS_STILL_IN_FIELD,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Module-level state set by configure()
_templates: Jinja2Templates | None = None
_images_path: Path | None = None

MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MB
_ALLOWED_CONTENT_TYPES = ("image/jpeg", "image/png")
_CONTENT_TYPE_EXT = {"image/jpeg": "jpg", "image/png": "png"}


def configure(templates: Jinja2Templates, images_path: Path) -> None:
    """Set module-level dependencies. Called once during app startup."""
    global _templates, _images_path
    _templates = templates
    _images_path = images_path
    # Ensure the image store exists so uploads succeed on a fresh deployment.
    try:
        images_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning("Could not create found rockets image store %s: %s", images_path, exc)


def _is_admin(user) -> bool:
    return bool(user) and getattr(user, "role", None) in ("admin", "data_entry")


async def _read_upload(upload) -> tuple[bytes, str]:
    """Validate an uploaded image and return (bytes, extension).

    Raises HTTPException on validation failure.
    """
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(status_code=400, detail="No image provided")

    content_type = (getattr(upload, "content_type", "") or "").lower()
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Only JPEG and PNG are accepted.",
        )

    file_bytes = await upload.read(MAX_IMAGE_BYTES + 1)
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(file_bytes) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail="File too large. Maximum upload size is 20 MB.",
        )

    return file_bytes, _CONTENT_TYPE_EXT[content_type]


def _parse_coordinate(raw, name: str, lo: float, hi: float) -> float | None:
    """Parse an optional coordinate form field into a bounded float or None."""
    if raw is None:
        return None
    raw = str(raw).strip()
    if raw == "":
        return None
    try:
        value = float(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name} value")
    if not (lo <= value <= hi):
        raise HTTPException(
            status_code=400, detail=f"{name} must be between {lo} and {hi}"
        )
    return value


# ---------------------------------------------------------------------------
# Listing page
# ---------------------------------------------------------------------------


@router.get("/found-rockets", response_class=HTMLResponse)
async def found_rockets_page(
    request: Request,
    db: AsyncSession = Depends(get_found_rockets_db),
) -> HTMLResponse:
    """Render the found rockets listing page.

    Reunited rockets are always excluded. Non-admins only see rockets whose
    images have been approved; admins see all (approved or not).
    """
    if _templates is None:
        raise RuntimeError("Found rockets router not configured.")

    current_user = getattr(request.state, "user", None)

    result = await db.execute(
        select(FoundRocket)
        .where(FoundRocket.reunited.is_(False))
        .order_by(FoundRocket.added_at.desc())
    )
    rockets = result.scalars().all()

    return _templates.TemplateResponse(
        name="found_rockets.html",
        request=request,
        context={
            "page_title": "Found Rockets",
            "rockets": rockets,
            "current_user": current_user,
        },
    )


# ---------------------------------------------------------------------------
# Report form
# ---------------------------------------------------------------------------


@router.get(
    "/found-rockets/report",
    response_class=HTMLResponse,
    dependencies=[Depends(require_role(Role.FLYER))],
)
async def found_rocket_report_form(request: Request) -> HTMLResponse:
    """Render the found rocket report form (requires FLYER)."""
    if _templates is None:
        raise RuntimeError("Found rockets router not configured.")

    return _templates.TemplateResponse(
        name="found_rocket_report.html",
        request=request,
        context={
            "page_title": "Report a Found Rocket",
            "current_user": getattr(request.state, "user", None),
        },
    )


# ---------------------------------------------------------------------------
# EXIF GPS extraction (for client-side autofill)
# ---------------------------------------------------------------------------


@router.post(
    "/found-rockets/api/extract-gps",
    dependencies=[Depends(require_role(Role.FLYER))],
)
async def extract_gps(request: Request):
    """Extract GPS coordinates from an uploaded image's EXIF metadata.

    Used by the report form to pre-fill the latitude/longitude fields before
    the finalized submission. Returns nulls when no GPS data is present.
    """
    from ..services.exif_service import extract_gps_coordinates

    form = await request.form()
    upload = form.get("image")
    file_bytes, _ext = await _read_upload(upload)

    coords = extract_gps_coordinates(file_bytes)
    if coords is None:
        return {"found": False, "latitude": None, "longitude": None}
    return {
        "found": True,
        "latitude": coords.latitude,
        "longitude": coords.longitude,
    }


# ---------------------------------------------------------------------------
# Submit a found rocket
# ---------------------------------------------------------------------------


@router.post(
    "/found-rockets/api/report",
    dependencies=[Depends(require_role(Role.FLYER))],
)
async def submit_found_rocket(
    request: Request,
    db: AsyncSession = Depends(get_found_rockets_db),
):
    """Create a found rocket report with an uploaded (pending-approval) image.

    The image's EXIF GPS is used to default the location when the finder does
    not supply explicit latitude/longitude values.
    """
    if _images_path is None:
        raise RuntimeError("Found rockets router not configured.")

    from ..services.exif_service import extract_gps_coordinates
    from ..services.found_rocket_image_service import (
        delete_found_rocket_image,
        generate_image_token,
        save_found_rocket_image,
    )

    form = await request.form()

    # Early Content-Length guard before reading the body.
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_IMAGE_BYTES + 1024 * 1024:
        raise HTTPException(
            status_code=413, detail="File too large. Maximum upload size is 20 MB."
        )

    upload = form.get("image")
    file_bytes, ext = await _read_upload(upload)

    description = form.get("description")
    if description is not None:
        description = str(description).strip() or None

    # Status: still in field vs recovered.
    status_raw = str(form.get("status", STATUS_STILL_IN_FIELD)).strip()
    if status_raw not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status value")

    # Coordinates: explicit values win; otherwise default from EXIF.
    latitude = _parse_coordinate(form.get("latitude"), "latitude", -90.0, 90.0)
    longitude = _parse_coordinate(form.get("longitude"), "longitude", -180.0, 180.0)

    if latitude is None and longitude is None:
        coords = extract_gps_coordinates(file_bytes)
        if coords is not None:
            latitude = coords.latitude
            longitude = coords.longitude

    user = getattr(request.state, "user", None)
    found_by = user.email if user else "unknown"

    # Save the image with an unguessable tokenized filename.
    token = generate_image_token()
    image_filename = save_found_rocket_image(file_bytes, ext, _images_path, token)

    rocket = FoundRocket(
        description=description,
        latitude=latitude,
        longitude=longitude,
        status=status_raw,
        image_path=image_filename,
        image_token=token,
        approved=False,
        reunited=False,
        found_by=found_by,
    )
    db.add(rocket)
    try:
        await db.commit()
    except Exception:
        # Roll back the orphaned image file if the DB write fails.
        delete_found_rocket_image(_images_path, image_filename)
        raise
    await db.refresh(rocket)

    return {
        "message": "Found rocket reported. It will be visible once an admin approves the photo.",
        "id": rocket.id,
        "status": rocket.status,
        "latitude": rocket.latitude,
        "longitude": rocket.longitude,
    }


# ---------------------------------------------------------------------------
# Reunite (poster or admin) — removes from the public list
# ---------------------------------------------------------------------------


@router.post(
    "/found-rockets/api/{found_id}/reunite",
    dependencies=[Depends(require_role(Role.FLYER))],
)
async def reunite_found_rocket(
    request: Request,
    found_id: int,
    db: AsyncSession = Depends(get_found_rockets_db),
):
    """Mark a found rocket as reunited with its flier.

    Authorization: the original poster (matched by email) or an
    admin/data_entry user. This removes the rocket from the public listing.
    """
    result = await db.execute(
        select(FoundRocket).where(FoundRocket.id == found_id)
    )
    rocket = result.scalar_one_or_none()
    if rocket is None:
        raise HTTPException(status_code=404, detail="Found rocket not found")

    user = getattr(request.state, "user", None)
    user_role = getattr(user, "role", None)
    user_email = getattr(user, "email", None)
    if user_role not in ("admin", "data_entry") and user_email != rocket.found_by:
        raise HTTPException(
            status_code=403,
            detail="Only the poster or an admin can mark this rocket as reunited.",
        )

    rocket.reunited = True
    await db.commit()

    return {"message": "Found rocket marked as reunited.", "id": found_id}


# ---------------------------------------------------------------------------
# Serve a found rocket image (gated by approval)
# ---------------------------------------------------------------------------


@router.get("/found-rockets/images/{filename:path}")
async def serve_found_rocket_image(
    request: Request,
    filename: str,
    db: AsyncSession = Depends(get_found_rockets_db),
) -> FileResponse:
    """Serve a found rocket image from the found rockets image store.

    The tokenized filename is unguessable, but we additionally require that the
    associated record is approved before serving to non-admins — so that even a
    leaked-but-unapproved URL is not viewable by the public.
    """
    if _images_path is None:
        raise RuntimeError("Found rockets router not configured.")

    # Prevent path traversal — only serve a bare filename.
    if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
        raise HTTPException(status_code=404, detail="Image not found")

    result = await db.execute(
        select(FoundRocket).where(FoundRocket.image_path == filename)
    )
    rocket = result.scalar_one_or_none()
    if rocket is None:
        raise HTTPException(status_code=404, detail="Image not found")

    user = getattr(request.state, "user", None)
    if not rocket.approved and not _is_admin(user):
        # Do not reveal existence of unapproved images to the public.
        raise HTTPException(status_code=404, detail="Image not found")

    image_path = _images_path / filename
    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(str(image_path))
