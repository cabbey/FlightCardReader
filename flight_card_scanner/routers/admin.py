"""Admin API router (mode switch, trigger, re-queue, motor resolution).

Provides endpoints for:
- Switching extraction mode (immediate/deferred)
- Manually triggering extraction of pending records
- Requeuing failed records (all or by ID)
- Motor resolution via ThrustCurve API
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import AppConfig
from ..database import get_db
from ..dependencies.auth import Role, require_role
from ..schemas import (
    FlightRecordUpdate,
    ModeResponse,
    RequeueResponse,
    SetModeRequest,
    TriggerResponse,
)
from ..services import record_service
from ..services.audit_service import log_action
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


@router.post("/mode", response_model=ModeResponse, dependencies=[Depends(require_role(Role.DATA_ENTRY))])
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


@router.post("/trigger", response_model=TriggerResponse, dependencies=[Depends(require_role(Role.DATA_ENTRY))])
async def trigger_extraction(
    request: Request,
    extraction_service: ExtractionService = Depends(get_extraction_service),
) -> TriggerResponse:
    """Manually trigger extraction of all pending records."""
    dispatched = await extraction_service.trigger_pending()

    # Audit log the trigger action
    user = getattr(request.state, "user", None)
    actor = user.email if user else "anonymous"
    log_action(actor, "extracted", "flight_record", 0, details={"dispatched": dispatched})

    return TriggerResponse(dispatched=dispatched)


@router.post("/requeue", response_model=RequeueResponse, dependencies=[Depends(require_role(Role.DATA_ENTRY))])
async def requeue_all_failed(
    request: Request,
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

    # Audit log the requeue action
    user = getattr(request.state, "user", None)
    actor = user.email if user else "anonymous"
    for record in failed_records:
        log_action(actor, "requeued", "flight_record", record.id)

    return RequeueResponse(requeued=len(failed_records))


@router.post("/requeue/{record_id}", response_model=RequeueResponse, dependencies=[Depends(require_role(Role.DATA_ENTRY))])
async def requeue_single(
    request: Request,
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

    # Audit log the requeue action
    user = getattr(request.state, "user", None)
    actor = user.email if user else "anonymous"
    log_action(actor, "requeued", "flight_record", record_id)

    return RequeueResponse(requeued=1)


@router.post("/extract/{record_id}", response_model=TriggerResponse, dependencies=[Depends(require_role(Role.DATA_ENTRY))])
async def extract_single(
    request: Request,
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

    # Audit log the extraction
    user = getattr(request.state, "user", None)
    actor = user.email if user else "anonymous"
    log_action(actor, "extracted", "flight_record", record_id)

    return TriggerResponse(dispatched=1)


@router.put("/record/{record_id}", dependencies=[Depends(require_role(Role.DATA_ENTRY))])
async def update_record(
    request: Request,
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

    # Capture old values for audit logging
    old_values = {}
    for field_name in updates:
        old_values[field_name] = getattr(record, field_name, None)

    updated_record = await record_service.update_fields(db, record_id, updates)

    # Audit log the update with old/new field changes
    user = getattr(request.state, "user", None)
    actor = user.email if user else "anonymous"
    changes = {}
    for field_name, new_value in updates.items():
        old_value = old_values.get(field_name)
        if old_value != new_value:
            changes[field_name] = {"old": old_value, "new": new_value}
    if changes:
        log_action(actor, "updated", "flight_record", record_id, details={"changes": changes})

    # Re-run flier verification if flier_name or membership fields changed
    should_reverify = "flier_name" in updates
    if not should_reverify and "overflow" in updates and updates["overflow"]:
        new_membership = updates["overflow"].get("membership")
        if new_membership:
            old_membership = (record.overflow or {}).get("membership", {})
            if (new_membership.get("club") != old_membership.get("club") or
                    new_membership.get("member_number") != old_membership.get("member_number")):
                should_reverify = True

    if should_reverify and _flier_match_service and _flier_match_service.enabled:
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
        overflow.pop("membership", None)
    elif not result.matched:
        overflow["flier_match_status"] = "not_found"
        overflow.pop("flier_match_error", None)
        record.flier_verified = False
        overflow.pop("membership", None)
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


@router.post("/record/{record_id}/motor/{motor_index}/search", dependencies=[Depends(require_role(Role.DATA_ENTRY))])
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


@router.post("/record/{record_id}/motor/{motor_index}/select", dependencies=[Depends(require_role(Role.DATA_ENTRY))])
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


@router.get("/next-unverified")
async def next_unverified(
    after: int = Query(default=0),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return the ID of the next unverified record closest to *after*.

    Only considers records where extraction_status is 'extracted' (ready for
    human review) and human_verified is False.

    Finds the closest record by ID to the given `after` value, checking both
    the next higher ID and the next lower ID and returning whichever is
    numerically closer.

    Returns {"id": <int>} if found, or {"id": null} if none remain.
    """
    from sqlalchemy import select as sa_select
    from ..models import FlightRecord

    base_filters = [
        FlightRecord.human_verified == False,  # noqa: E712
        FlightRecord.extraction_status == "extracted",
    ]
    if after:
        base_filters.append(FlightRecord.id != after)

    # Find the closest candidate with a higher ID
    stmt_next = (
        sa_select(FlightRecord.id)
        .where(*base_filters, FlightRecord.id > after)
        .order_by(FlightRecord.id.asc())
        .limit(1)
    )
    # Find the closest candidate with a lower ID
    stmt_prev = (
        sa_select(FlightRecord.id)
        .where(*base_filters, FlightRecord.id < after)
        .order_by(FlightRecord.id.desc())
        .limit(1)
    )

    next_result = await db.execute(stmt_next)
    next_id = next_result.scalar_one_or_none()

    prev_result = await db.execute(stmt_prev)
    prev_id = prev_result.scalar_one_or_none()

    # Pick whichever is closest to `after`
    if next_id is not None and prev_id is not None:
        chosen = next_id if (next_id - after) <= (after - prev_id) else prev_id
    elif next_id is not None:
        chosen = next_id
    elif prev_id is not None:
        chosen = prev_id
    else:
        chosen = None

    return {"id": chosen}


@router.get("/queue")
async def get_queue(request: Request) -> dict:
    """Return the list of record IDs currently in the extraction queue."""
    extraction_service = request.app.state.extraction_service
    queued = sorted(extraction_service.queued_ids)
    return {"queued_ids": queued, "count": len(queued)}


@router.get("/stats")
async def get_stats(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return status counts and verification stats for the site-wide header.

    No auth required — just aggregate counts, no sensitive data.
    """
    from sqlalchemy import func, select

    from ..models import FlightRecord

    # Status counts
    count_stmt = (
        select(FlightRecord.extraction_status, func.count(FlightRecord.id))
        .group_by(FlightRecord.extraction_status)
    )
    count_result = await db.execute(count_stmt)
    status_counts = {"pending": 0, "processing": 0, "extracted": 0, "extraction_failed": 0}
    for st, count in count_result.all():
        if st in status_counts:
            status_counts[st] = count

    # Verified counts
    verified_stmt = select(func.count(FlightRecord.id)).where(
        FlightRecord.human_verified == True  # noqa: E712
    )
    verified_result = await db.execute(verified_stmt)
    verified_count = verified_result.scalar() or 0

    total_stmt = select(func.count(FlightRecord.id))
    total_result = await db.execute(total_stmt)
    total_all = total_result.scalar() or 0

    verified_percent = round((verified_count / total_all * 100) if total_all > 0 else 0, 1)

    # Current extraction mode
    extraction_service = _extraction_service
    current_mode = extraction_service.mode.value if extraction_service else "unknown"

    return {
        "status_counts": status_counts,
        "verified_count": verified_count,
        "total_all": total_all,
        "verified_percent": verified_percent,
        "current_mode": current_mode,
    }


@router.delete("/record/{record_id}", dependencies=[Depends(require_role(Role.ADMIN))])
async def delete_record(
    request: Request,
    record_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete a flight record permanently (e.g. redundant/duplicate card).

    Returns 404 if the record does not exist.
    """
    record = await record_service.get(db, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")

    await db.delete(record)
    await db.commit()

    # Audit log the deletion
    user = getattr(request.state, "user", None)
    actor = user.email if user else "anonymous"
    log_action(actor, "deleted", "flight_record", record_id)

    return {"message": "Record deleted", "id": record_id}


# ---------------------------------------------------------------------------
# Implementation functions for event-scoped routes
# ---------------------------------------------------------------------------


async def set_mode_impl(request: Request, extraction_service) -> dict:
    """Shared implementation for switching extraction mode."""
    body = await request.json()
    mode_str = body.get("mode", "")
    try:
        mode = ExtractionMode(mode_str)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid mode '{mode_str}'. Must be 'immediate' or 'deferred'.",
        )
    await extraction_service.set_mode(mode)
    return {"mode": mode.value, "message": f"Mode set to {mode.value}"}


async def trigger_extraction_impl(request: Request, extraction_service) -> dict:
    """Shared implementation for triggering extraction."""
    dispatched = await extraction_service.trigger_pending()
    user = getattr(request.state, "user", None)
    actor = user.email if user else "anonymous"
    log_action(actor, "extracted", "flight_record", 0, details={"dispatched": dispatched})
    return {"dispatched": dispatched}


async def requeue_all_failed_impl(request: Request, db: AsyncSession, extraction_service) -> dict:
    """Shared implementation for requeuing all failed records."""
    failed_records = await record_service.get_by_status(db, "extraction_failed")
    for record in failed_records:
        await record_service.set_status(db, record.id, "pending")
        await extraction_service.enqueue(record.id)
    user = getattr(request.state, "user", None)
    actor = user.email if user else "anonymous"
    for record in failed_records:
        log_action(actor, "requeued", "flight_record", record.id)
    return {"requeued": len(failed_records)}


async def requeue_single_impl(request: Request, record_id: int, db: AsyncSession, extraction_service) -> dict:
    """Shared implementation for requeuing a single failed record."""
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
    user = getattr(request.state, "user", None)
    actor = user.email if user else "anonymous"
    log_action(actor, "requeued", "flight_record", record_id)
    return {"requeued": 1}


async def extract_single_impl(request: Request, record_id: int, db: AsyncSession, extraction_service) -> dict:
    """Shared implementation for forcing extraction of a single record."""
    record = await record_service.get(db, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")
    await record_service.set_status(db, record.id, "pending")
    await extraction_service.force_enqueue(record.id)
    user = getattr(request.state, "user", None)
    actor = user.email if user else "anonymous"
    log_action(actor, "extracted", "flight_record", record_id)
    return {"dispatched": 1}


async def update_record_impl(request: Request, record_id: int, db: AsyncSession, config, flier_match_service) -> dict:
    """Shared implementation for updating a flight record."""
    body_data = await request.json()
    body = FlightRecordUpdate(**body_data)

    record = await record_service.get(db, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    old_values = {}
    for field_name in updates:
        old_values[field_name] = getattr(record, field_name, None)

    updated_record = await record_service.update_fields(db, record_id, updates)

    user = getattr(request.state, "user", None)
    actor = user.email if user else "anonymous"
    changes = {}
    for field_name, new_value in updates.items():
        old_value = old_values.get(field_name)
        if old_value != new_value:
            changes[field_name] = {"old": old_value, "new": new_value}
    if changes:
        log_action(actor, "updated", "flight_record", record_id, details={"changes": changes})

    # Re-run flier verification if needed
    should_reverify = "flier_name" in updates
    if not should_reverify and "overflow" in updates and updates["overflow"]:
        new_membership = updates["overflow"].get("membership")
        if new_membership:
            old_membership = (record.overflow or {}).get("membership", {})
            if (new_membership.get("club") != old_membership.get("club") or
                    new_membership.get("member_number") != old_membership.get("member_number")):
                should_reverify = True

    if should_reverify and flier_match_service and flier_match_service.enabled:
        await _run_flier_verification_with_service(db, updated_record, flier_match_service, config)

    return {"message": "Record updated", "id": updated_record.id}


async def _run_flier_verification_with_service(db: AsyncSession, record, flier_match_service, config) -> None:
    """Run flier match verification with explicit service reference."""
    membership = (record.overflow or {}).get("membership", {})

    try:
        result = await flier_match_service.match_flier(
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
        overflow.pop("membership", None)
    elif not result.matched:
        overflow["flier_match_status"] = "not_found"
        overflow.pop("flier_match_error", None)
        record.flier_verified = False
        overflow.pop("membership", None)
    else:
        overflow.pop("flier_match_error", None)
        row = result.row_data
        roster_data = flier_match_service.extract_roster_data(row)
        record.flier_name = roster_data["name"] or record.flier_name

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
        overflow["flier_match_confidence"] = result.confidence

        auto_accept_threshold = getattr(config, "auto_accept_threshold", 0.95)
        if result.confidence > auto_accept_threshold:
            overflow["flier_match_status"] = "verified"
            record.flier_verified = True
        else:
            overflow["flier_match_status"] = "review"
            record.flier_verified = False

    record.overflow = overflow
    await db.commit()


async def search_motor_impl(request: Request, record_id: int, motor_index: int, db: AsyncSession, thrustcurve_service) -> dict:
    """Shared implementation for motor search."""
    body_data = await request.json()
    body = MotorSearchRequest(**body_data)

    record = await record_service.get(db, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")

    if thrustcurve_service is None:
        raise HTTPException(status_code=503, detail="ThrustCurve service not available")

    search_motor_data = {
        "letter": body.letter,
        "number": body.number,
        "manufacturer": body.manufacturer,
        "suffix": body.suffix,
    }

    result = await thrustcurve_service.search_motor(search_motor_data)

    motor_data = None
    if result["motorId"]:
        motor_data = await thrustcurve_service.get_motor(result["motorId"])

    return {
        "motorId": result["motorId"],
        "motorData": motor_data,
        "candidates": result.get("candidates", []),
        "error": result.get("error"),
        "query": result.get("query"),
    }


async def select_motor_impl(request: Request, motor_id, thrustcurve_service) -> dict:
    """Shared implementation for motor selection."""
    body_data = await request.json()
    body = MotorSelectRequest(**body_data)

    motor_data = None
    if thrustcurve_service:
        motor_data = await thrustcurve_service.get_motor(body.motor_id)

    return {
        "message": "Motor validated",
        "motorId": body.motor_id,
        "motorData": motor_data,
    }


async def debug_record_impl(record_id: int, db: AsyncSession) -> dict:
    """Shared implementation for debug record endpoint."""
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


def debug_flier_service_impl(flier_match_service) -> dict:
    """Shared implementation for debug flier service endpoint."""
    if flier_match_service is None:
        return {"enabled": False, "reason": "FlierMatchService not configured"}
    return {
        "enabled": flier_match_service.enabled,
        "row_count": flier_match_service.row_count,
        "headers": flier_match_service._headers,
        "sample_rows": flier_match_service._rows[:5],
    }


async def next_unverified_impl(after: int, db: AsyncSession) -> dict:
    """Shared implementation for finding next unverified record."""
    from sqlalchemy import select as sa_select
    from ..models import FlightRecord

    base_filters = [
        FlightRecord.human_verified == False,  # noqa: E712
        FlightRecord.extraction_status == "extracted",
    ]
    if after:
        base_filters.append(FlightRecord.id != after)

    stmt_next = (
        sa_select(FlightRecord.id)
        .where(*base_filters, FlightRecord.id > after)
        .order_by(FlightRecord.id.asc())
        .limit(1)
    )
    stmt_prev = (
        sa_select(FlightRecord.id)
        .where(*base_filters, FlightRecord.id < after)
        .order_by(FlightRecord.id.desc())
        .limit(1)
    )

    next_result = await db.execute(stmt_next)
    next_id = next_result.scalar_one_or_none()

    prev_result = await db.execute(stmt_prev)
    prev_id = prev_result.scalar_one_or_none()

    if next_id is not None and prev_id is not None:
        chosen = next_id if (next_id - after) <= (after - prev_id) else prev_id
    elif next_id is not None:
        chosen = next_id
    elif prev_id is not None:
        chosen = prev_id
    else:
        chosen = None

    return {"id": chosen}


def get_queue_impl(extraction_service) -> dict:
    """Shared implementation for getting the extraction queue."""
    queued = sorted(extraction_service.queued_ids) if extraction_service else []
    return {"queued_ids": queued, "count": len(queued)}


async def get_stats_impl(db: AsyncSession, extraction_service) -> dict:
    """Shared implementation for getting stats."""
    from sqlalchemy import func, select
    from ..models import FlightRecord

    count_stmt = (
        select(FlightRecord.extraction_status, func.count(FlightRecord.id))
        .group_by(FlightRecord.extraction_status)
    )
    count_result = await db.execute(count_stmt)
    status_counts = {"pending": 0, "processing": 0, "extracted": 0, "extraction_failed": 0}
    for st, count in count_result.all():
        if st in status_counts:
            status_counts[st] = count

    verified_stmt = select(func.count(FlightRecord.id)).where(
        FlightRecord.human_verified == True  # noqa: E712
    )
    verified_result = await db.execute(verified_stmt)
    verified_count = verified_result.scalar() or 0

    total_stmt = select(func.count(FlightRecord.id))
    total_result = await db.execute(total_stmt)
    total_all = total_result.scalar() or 0

    verified_percent = round((verified_count / total_all * 100) if total_all > 0 else 0, 1)

    current_mode = extraction_service.mode.value if extraction_service else "unknown"

    return {
        "status_counts": status_counts,
        "verified_count": verified_count,
        "total_all": total_all,
        "verified_percent": verified_percent,
        "current_mode": current_mode,
    }


async def delete_record_impl(request: Request, record_id: int, db: AsyncSession) -> dict:
    """Shared implementation for deleting a record."""
    record = await record_service.get(db, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")

    await db.delete(record)
    await db.commit()

    user = getattr(request.state, "user", None)
    actor = user.email if user else "anonymous"
    log_action(actor, "deleted", "flight_record", record_id)

    return {"message": "Record deleted", "id": record_id}
