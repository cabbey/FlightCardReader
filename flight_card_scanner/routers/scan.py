"""POST /scan router.

Handles card image uploads: validates the file type, saves the image,
creates a FlightRecord, and enqueues the record for extraction.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import AppConfig
from ..database import get_db
from ..exceptions import ImageStorageError
from ..schemas import ScanResponse
from ..services import image_service, record_service
from ..services.extraction_service import ExtractionService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dependency helpers (simple module-level state; wired up in main.py lifespan)
# ---------------------------------------------------------------------------

_config: AppConfig | None = None
_extraction_service: ExtractionService | None = None


def configure(config: AppConfig, extraction_service: ExtractionService) -> None:
    """Set module-level dependencies. Called once during app startup."""
    global _config, _extraction_service
    _config = config
    _extraction_service = extraction_service


def get_config() -> AppConfig:
    """FastAPI dependency that returns the application config."""
    if _config is None:
        raise RuntimeError("Scan router not configured. Call configure() at startup.")
    return _config


def get_extraction_service() -> ExtractionService:
    """FastAPI dependency that returns the ExtractionService instance."""
    if _extraction_service is None:
        raise RuntimeError("Scan router not configured. Call configure() at startup.")
    return _extraction_service


# ---------------------------------------------------------------------------
# Allowed content types and extensions
# ---------------------------------------------------------------------------

_ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png"}
_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png"}

# Map content type to file extension for save_image
_CONTENT_TYPE_TO_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
}


def _resolve_extension(upload: UploadFile) -> str | None:
    """Determine the file extension from content type or filename.

    Returns the extension (without dot) if the file is an allowed type,
    or None if validation fails.
    """
    # Primary check: content type
    if upload.content_type in _ALLOWED_CONTENT_TYPES:
        return _CONTENT_TYPE_TO_EXT[upload.content_type]

    # Fallback: check filename extension
    if upload.filename:
        suffix = Path(upload.filename).suffix.lower()
        if suffix in _ALLOWED_EXTENSIONS:
            return "jpg" if suffix in {".jpg", ".jpeg"} else "png"

    return None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter()


@router.post("/scan", status_code=201, response_model=ScanResponse)
async def submit_card(
    card_image: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    config: AppConfig = Depends(get_config),
    extraction_service: ExtractionService = Depends(get_extraction_service),
) -> ScanResponse:
    """Accept a card image upload and create a pending flight record.

    1. Validate file is JPEG or PNG.
    2. Save image to the Image Store.
    3. Create a FlightRecord (status=pending).
    4. Enqueue the record for extraction (no-op in DEFERRED mode).
    5. Return 201 with the record ID.
    """
    # --- 1. Validate file type ---
    ext = _resolve_extension(card_image)
    if ext is None:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Only JPEG and PNG images are accepted.",
        )

    # --- 2. Save image ---
    file_bytes = await card_image.read()
    try:
        filename = image_service.save_image(
            file_bytes=file_bytes,
            ext=ext,
            store_path=config.image_store_path,
        )
    except ImageStorageError as exc:
        logger.error("Failed to save uploaded image: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="Failed to store the uploaded image.",
        ) from exc

    # --- 3. Create DB record ---
    try:
        record = await record_service.create(db, image_path=filename)
    except Exception as exc:
        # Rollback: delete the saved image since the record wasn't created
        logger.error("Failed to create flight record: %s", exc)
        image_service.delete_image(config.image_store_path / filename)
        raise HTTPException(
            status_code=500,
            detail="Failed to create flight record.",
        ) from exc

    # --- 4. Enqueue for extraction ---
    await extraction_service.enqueue(record.id)

    # --- 5. Return success ---
    return ScanResponse(record_id=record.id)
