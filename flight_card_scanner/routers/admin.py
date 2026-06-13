"""Admin API router (mode switch, trigger, re-queue).

Provides endpoints for:
- Switching extraction mode (immediate/deferred)
- Manually triggering extraction of pending records
- Requeuing failed records (all or by ID)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..schemas import (
    FlightRecordUpdate,
    ModeResponse,
    RequeueResponse,
    SetModeRequest,
    TriggerResponse,
)
from ..services import record_service
from ..services.extraction_service import ExtractionMode, ExtractionService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dependency helpers (module-level state; wired up in main.py lifespan)
# ---------------------------------------------------------------------------

_extraction_service: ExtractionService | None = None


def configure(extraction_service: ExtractionService) -> None:
    """Set module-level dependencies. Called once during app startup."""
    global _extraction_service
    _extraction_service = extraction_service


def get_extraction_service() -> ExtractionService:
    """FastAPI dependency that returns the ExtractionService instance."""
    if _extraction_service is None:
        raise RuntimeError("Admin router not configured. Call configure() at startup.")
    return _extraction_service


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/admin")


@router.post("/mode", response_model=ModeResponse)
async def set_mode(
    body: SetModeRequest,
    extraction_service: ExtractionService = Depends(get_extraction_service),
) -> ModeResponse:
    """Switch the extraction operating mode (immediate or deferred).

    If switching from deferred to immediate, pending records are
    automatically dispatched.
    """
    try:
        mode = ExtractionMode(body.mode)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid mode '{body.mode}'. Must be 'immediate' or 'deferred'.",
        )
    await extraction_service.set_mode(mode)
    return ModeResponse(mode=mode.value, message=f"Mode set to {mode.value}")


@router.post("/trigger", response_model=TriggerResponse)
async def trigger_extraction(
    extraction_service: ExtractionService = Depends(get_extraction_service),
) -> TriggerResponse:
    """Manually trigger extraction of all pending records."""
    dispatched = await extraction_service.trigger_pending()
    return TriggerResponse(dispatched=dispatched)


@router.post("/requeue", response_model=RequeueResponse)
async def requeue_all_failed(
    db: AsyncSession = Depends(get_db),
    extraction_service: ExtractionService = Depends(get_extraction_service),
) -> RequeueResponse:
    """Reset all extraction_failed records to pending and enqueue if immediate.

    Returns the count of records requeued.
    """
    failed_records = await record_service.get_by_status(db, "extraction_failed")
    for record in failed_records:
        await record_service.set_status(db, record.id, "pending")
        await extraction_service.enqueue(record.id)
    return RequeueResponse(requeued=len(failed_records))


@router.post("/requeue/{record_id}", response_model=RequeueResponse)
async def requeue_single(
    record_id: int,
    db: AsyncSession = Depends(get_db),
    extraction_service: ExtractionService = Depends(get_extraction_service),
) -> RequeueResponse:
    """Reset a single extraction_failed record to pending and enqueue if immediate.

    Returns 404 if the record does not exist.
    Returns 422 if the record is not in extraction_failed status.
    """
    record = await record_service.get(db, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")
    if record.extraction_status != "extraction_failed":
        raise HTTPException(
            status_code=422,
            detail=f"Record status is '{record.extraction_status}', not 'extraction_failed'",
        )
    await record_service.set_status(db, record.id, "pending")
    await extraction_service.enqueue(record.id)
    return RequeueResponse(requeued=1)


@router.post("/extract/{record_id}", response_model=TriggerResponse)
async def extract_single(
    record_id: int,
    db: AsyncSession = Depends(get_db),
    extraction_service: ExtractionService = Depends(get_extraction_service),
) -> TriggerResponse:
    """Force extraction of a single record regardless of its current status.

    Sets the record to pending and enqueues it for extraction.
    Returns 404 if the record does not exist.
    """
    record = await record_service.get(db, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")
    await record_service.set_status(db, record.id, "pending")
    await extraction_service.force_enqueue(record.id)
    return TriggerResponse(dispatched=1)


@router.put("/record/{record_id}")
async def update_record(
    record_id: int,
    body: FlightRecordUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update editable fields on a flight record (human review corrections).

    Only fields provided in the request body are updated.
    Returns 404 if the record does not exist.
    """
    record = await record_service.get(db, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")

    # Only include fields that were explicitly set in the request
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    updated_record = await record_service.update_fields(db, record_id, updates)
    return {"message": "Record updated", "id": updated_record.id}
