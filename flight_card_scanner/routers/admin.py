"""Admin API router (mode switch, trigger, re-queue, motor resolution).

Provides endpoints for:
- Switching extraction mode (immediate/deferred)
- Manually triggering extraction of pending records
- Requeuing failed records (all or by ID)
- Motor resolution via ThrustCurve API
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import AppConfig
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
from ..services.flier_match_service import FlierMatchService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dependency helpers (module-level state; wired up in main.py lifespan)
# ---------------------------------------------------------------------------

_extraction_service: ExtractionService | None = None
_flier_match_service: FlierMatchService | None = None
_config: AppConfig | None = None


def configure(
    extraction_service: ExtractionService,
    flier_match_service: FlierMatchService | None = None,
    config: AppConfig | None = None,
) -> None:
    """Set module-level dependencies. Called once during app startup."""
    global _extraction_service, _flier_match_service, _config
    _extraction_service = extraction_service
    _flier_match_service = flier_match_service
    _config = config


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

    # Re-run flier verification if flier_name was changed
    if "flier_name" in updates and _flier_match_service and _flier_match_service.enabled:
        await _run_flier_verification(db, updated_record)

    return {"message": "Record updated", "id": updated_record.id}


async def _run_flier_verification(db: AsyncSession, record) -> None:
    """Run flier match verification and store result in overflow."""
    membership = (record.overflow or {}).get("membership", {})

    try:
        result = await _flier_match_service.match_flier(
            flier_name=record.flier_name,
            club=membership.get("club"),
            member_number=membership.get("member_number"),
            cert_level=membership.get("cert_level"),
        )
    except Exception as exc:
        logger.warning("Flier verification failed for record %d: %s", record.id, exc)
        return

    overflow = dict(record.overflow or {})

    if result.error:
        overflow["flier_match_status"] = "error"
        overflow["flier_match_error"] = str(result.error)
        record.flier_verified = False
    elif not result.matched:
        overflow["flier_match_status"] = "not_found"
        overflow.pop("flier_match_error", None)
        record.flier_verified = False
    else:
        overflow.pop("flier_match_error", None)
        row = result.row_data

        # Use the service to extract data using detected column names
        roster_data = _flier_match_service.extract_roster_data(row)

        # Apply roster name to the record
        record.flier_name = roster_data["name"] or record.flier_name

        # Store roster membership data in the format the system expects
        # (compatible with MembershipInfo schema and detail template)
        mem = {}
        nar_num = roster_data["nar_number"]
        tra_num = roster_data["tra_number"]
        mem["nar_number"] = nar_num
        mem["tra_number"] = tra_num
        if nar_num:
            mem["club"] = "NAR"
            mem["member_number"] = nar_num
        elif tra_num:
            mem["club"] = "TRA"
            mem["member_number"] = tra_num
        if roster_data["cert_level"] is not None:
            mem["cert_level"] = roster_data["cert_level"]
        overflow["membership"] = mem

        # Store confidence
        overflow["flier_match_confidence"] = result.confidence

        # Tier-specific: determine auto-accept threshold
        auto_accept_threshold = _config.auto_accept_threshold if _config else 0.95
        if result.confidence > auto_accept_threshold:
            overflow["flier_match_status"] = "verified"
            record.flier_verified = True
        else:
            overflow["flier_match_status"] = "review"
            record.flier_verified = False

    record.overflow = overflow
    await db.commit()


# ---------------------------------------------------------------------------
# Motor resolution endpoints
# ---------------------------------------------------------------------------


class MotorSearchRequest(BaseModel):
    """Request body for searching ThrustCurve for a motor."""

    letter: str
    number: str
    manufacturer: str | None = None
    suffix: str | None = None


class MotorSelectRequest(BaseModel):
    """Request body for selecting a specific motor from candidates."""

    motor_id: str


@router.post("/record/{record_id}/motor/{motor_index}/search")
async def search_motor(
    request: Request,
    record_id: int,
    motor_index: int,
    body: MotorSearchRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Search ThrustCurve for a motor using provided parameters.

    This is a pure search — it does NOT modify the record. The client
    tracks the result as a pending change and saves via the normal Save flow.
    """
    record = await record_service.get(db, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")

    tc_service = getattr(request.app.state, "thrustcurve_service", None)
    if tc_service is None:
        raise HTTPException(
            status_code=503, detail="ThrustCurve service not available"
        )

    overflow = dict(record.overflow) if record.overflow else {}
    motors = overflow.get("motors", [])

    if motor_index < 0 or motor_index >= len(motors):
        raise HTTPException(status_code=404, detail="Motor index out of range")

    # Build a motor dict from the request
    search_motor_data = {
        "letter": body.letter,
        "number": body.number,
        "manufacturer": body.manufacturer,
        "suffix": body.suffix,
    }

    result = await tc_service.search_motor(search_motor_data)

    # If unique match, include motor data for display
    motor_data = None
    if result["motorId"]:
        motor_data = await tc_service.get_motor(result["motorId"])

    return {
        "motorId": result["motorId"],
        "motorData": motor_data,
        "candidates": result.get("candidates", []),
        "error": result.get("error"),
        "query": result.get("query"),
    }


@router.post("/record/{record_id}/motor/{motor_index}/select")
async def select_motor(
    request: Request,
    record_id: int,
    motor_index: int,
    body: MotorSelectRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Validate a motor selection (does not persist — use Save for that).

    Returns motor data from the cache for display purposes.
    """
    tc_service = getattr(request.app.state, "thrustcurve_service", None)
    motor_data = None
    if tc_service:
        motor_data = await tc_service.get_motor(body.motor_id)

    return {
        "message": "Motor validated",
        "motorId": body.motor_id,
        "motorData": motor_data,
    }


@router.get("/debug/record/{record_id}")
async def debug_record(
    record_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Debug endpoint: dump the raw database record as JSON.

    Returns all columns including the full overflow JSON structure.
    """
    record = await record_service.get(db, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")

    return {
        "id": record.id,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "image_path": record.image_path,
        "extraction_status": record.extraction_status,
        "flight_date": record.flight_date.isoformat() if record.flight_date else None,
        "flier_name": record.flier_name,
        "total_impulse_value": record.total_impulse_value,
        "total_impulse_unit": record.total_impulse_unit,
        "flag_heads_up": record.flag_heads_up,
        "flag_first_flight": record.flag_first_flight,
        "flag_complex": record.flag_complex,
        "rack": record.rack,
        "pad": record.pad,
        "fso_rso_initials": record.fso_rso_initials,
        "evaluation_outcome": record.evaluation_outcome,
        "evaluation_comments": record.evaluation_comments,
        "recovery_plan": record.recovery_plan,
        "flier_verified": record.flier_verified,
        "human_verified": record.human_verified,
        "overflow": record.overflow,
    }


@router.get("/debug/flier-service")
async def debug_flier_service() -> dict:
    """Debug endpoint: dump the FlierMatchService state.

    Shows TSV headers, row count, enabled status, and the first 5 rows
    so we can verify column names match what the code expects.
    """
    if _flier_match_service is None:
        return {"enabled": False, "reason": "FlierMatchService not configured"}

    return {
        "enabled": _flier_match_service.enabled,
        "row_count": _flier_match_service.row_count,
        "headers": _flier_match_service._headers,
        "sample_rows": _flier_match_service._rows[:5],
    }
