"""Flight Record CRUD service.

Provides async functions for creating, querying, and updating FlightRecord
instances, including the apply_extraction helper that maps LLM output to
dedicated columns and overflow JSON.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import FlightRecord
from ..schemas import FlightCardExtraction


async def create(db: AsyncSession, image_path: str) -> FlightRecord:
    """Create a new FlightRecord with status 'pending'.

    Args:
        db: Active async database session.
        image_path: Relative path to the saved card image in the Image Store.

    Returns:
        The newly created FlightRecord instance.
    """
    record = FlightRecord(image_path=image_path, extraction_status="pending")
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


async def get(db: AsyncSession, record_id: int) -> Optional[FlightRecord]:
    """Fetch a single FlightRecord by primary key.

    Args:
        db: Active async database session.
        record_id: The integer primary key of the record.

    Returns:
        The FlightRecord if found, otherwise None.
    """
    result = await db.execute(
        select(FlightRecord).where(FlightRecord.id == record_id)
    )
    return result.scalar_one_or_none()


async def get_by_status(db: AsyncSession, status: str) -> list[FlightRecord]:
    """Fetch all FlightRecords matching a given extraction status.

    Args:
        db: Active async database session.
        status: The extraction_status value to filter on.

    Returns:
        A list of matching FlightRecord instances.
    """
    result = await db.execute(
        select(FlightRecord).where(FlightRecord.extraction_status == status)
    )
    return list(result.scalars().all())


async def set_status(db: AsyncSession, record_id: int, status: str) -> None:
    """Update the extraction_status of a FlightRecord.

    Args:
        db: Active async database session.
        record_id: The integer primary key of the record.
        status: The new extraction_status value.
    """
    record = await get(db, record_id)
    if record is not None:
        record.extraction_status = status
        await db.commit()


async def apply_extraction(
    db: AsyncSession,
    record_id: int,
    extracted: FlightCardExtraction,
    resolved_date: date | None,
) -> None:
    """Map extracted fields to dedicated columns and overflow JSON.

    Dedicated columns receive their mapped values directly. Remaining fields
    are collected into the overflow JSON column, omitting any keys whose
    values are None.

    After applying, sets extraction_status to 'extracted'.

    Args:
        db: Active async database session.
        record_id: The integer primary key of the record to update.
        extracted: The validated FlightCardExtraction from the LLM.
        resolved_date: The resolved calendar date (or None if unresolvable).
    """
    record = await get(db, record_id)
    if record is None:
        return

    # --- Store raw LLM output to JSON file alongside image ---
    # (handled by _call_ollama, not here)

    # --- Dedicated columns ---
    record.flight_date = resolved_date
    record.flier_name = extracted.flier_name
    record.total_impulse_value = extracted.total_impulse_value
    record.total_impulse_unit = extracted.total_impulse_unit
    record.flag_heads_up = extracted.flag_heads_up
    record.flag_first_flight = extracted.flag_first_flight
    record.flag_complex = extracted.flag_complex
    record.rack = extracted.rack
    record.pad = extracted.pad
    record.fso_rso_initials = extracted.fso_rso_initials
    record.evaluation_outcome = extracted.evaluation_outcome
    record.evaluation_comments = extracted.evaluation_comments
    record.recovery_plan = extracted.recovery_plan

    # --- Overflow JSON (only include non-None values) ---
    overflow: dict = {}

    if extracted.membership is not None:
        overflow["membership"] = extracted.membership.model_dump()

    if extracted.rocket_name is not None:
        overflow["rocket_name"] = extracted.rocket_name

    if extracted.rocket_manufacturer is not None:
        overflow["rocket_manufacturer"] = extracted.rocket_manufacturer

    if extracted.rocket_colors is not None:
        overflow["rocket_colors"] = extracted.rocket_colors

    if extracted.measurements is not None:
        measurements = extracted.measurements.model_dump()
        # Normalize " to "in" for units
        for unit_key in ("diameter_unit", "length_unit", "weight_unit"):
            if measurements.get(unit_key) == '"':
                measurements[unit_key] = "in"
        # If diameter or length is missing a unit but the other has one, share it
        d_unit = measurements.get("diameter_unit")
        l_unit = measurements.get("length_unit")
        if d_unit and not l_unit and measurements.get("length") is not None:
            measurements["length_unit"] = d_unit
        elif l_unit and not d_unit and measurements.get("diameter") is not None:
            measurements["diameter_unit"] = l_unit
        overflow["rocket_measurements"] = measurements

    if extracted.motors is not None:
        overflow["motors"] = [motor.model_dump() for motor in extracted.motors]

    if extracted.notes is not None:
        overflow["notes"] = extracted.notes

    if extracted.flight_date_raw is not None:
        overflow["raw_flight_date"] = extracted.flight_date_raw

    record.overflow = overflow if overflow else None

    # --- Mark as extracted ---
    record.extraction_status = "extracted"

    await db.commit()


async def update_fields(
    db: AsyncSession,
    record_id: int,
    updates: dict[str, Any],
) -> Optional[FlightRecord]:
    """Update specific fields on a FlightRecord.

    Only fields present in the updates dict are modified.

    Args:
        db: Active async database session.
        record_id: The integer primary key of the record to update.
        updates: A dict of field_name -> new_value pairs to apply.

    Returns:
        The updated FlightRecord if found, otherwise None.
    """
    record = await get(db, record_id)
    if record is None:
        return None

    # Fields that can be updated via human review
    editable_fields = {
        "flight_date",
        "flier_name",
        "total_impulse_value",
        "total_impulse_unit",
        "flag_heads_up",
        "flag_first_flight",
        "flag_complex",
        "rack",
        "pad",
        "fso_rso_initials",
        "evaluation_outcome",
        "evaluation_comments",
        "recovery_plan",
        "overflow",
    }

    for field, value in updates.items():
        if field in editable_fields:
            setattr(record, field, value)

    await db.commit()
    await db.refresh(record)
    return record


def _format_motor(motor: dict[str, Any]) -> str:
    """Format a single motor dict into a designation string.

    Format: [manufacturer ][[leading_number]-]letter+number[-suffix]

    Suffix rules:
    - If suffix starts with '-' or '/', use as-is
    - Otherwise prepend '-'
    """
    parts: list[str] = []

    # Core designation: [leading_number-]letter+number[-suffix]
    core = ""
    if motor.get("leading_number"):
        core += f"{motor['leading_number']}-"
    core += f"{motor['letter']}{motor['number']}"
    if motor.get("suffix"):
        suffix = motor["suffix"]
        if suffix.startswith("-") or suffix.startswith("/"):
            core += suffix
        else:
            core += f"-{suffix}"

    # Manufacturer prefix (space-separated from core)
    if motor.get("manufacturer"):
        parts.append(motor["manufacturer"])

    parts.append(core)
    return " ".join(parts)


def _format_stage(stage: list[dict[str, Any]]) -> str:
    """Format a stage (list of motors) into a designation string.

    Single motor: just the motor designation.
    Cluster (multiple motors): "{count}×{designation}" using the first motor.
    """
    if len(stage) == 1:
        return _format_motor(stage[0])
    # Cluster: use the first motor's designation with a count prefix
    return f"{len(stage)}×{_format_motor(stage[0])}"


def motor_designation_str(overflow: dict[str, Any] | None) -> str | None:
    """Return a human-readable motor designation string from overflow data.

    Examples:
        - Single motor: "AT M2560-WT"
        - Multiple motors: "AT M2560-WT / AT K600-WT"

    Args:
        overflow: The overflow dict from a FlightRecord, or None.

    Returns:
        The formatted designation string, or None if motors data is
        absent or empty.
    """
    if overflow is None:
        return None

    motors = overflow.get("motors")
    if not motors:
        return None

    # motors is a flat list of motor dicts (or legacy nested list of stages)
    # Detect legacy format: list of lists
    if motors and isinstance(motors[0], list):
        # Legacy nested format — flatten
        motor_strs: list[str] = []
        for stage in motors:
            for motor in stage:
                motor_strs.append(_format_motor(motor))
    else:
        # New flat format
        motor_strs = [_format_motor(m) for m in motors if m]

    if not motor_strs:
        return None

    return " / ".join(motor_strs)
