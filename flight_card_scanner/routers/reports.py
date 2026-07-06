"""Reports router (GET /reports and GET /reports/{date}).

Serves the overall event reporting page and per-day detail pages with
statistics on flights, motors, impulse, and flyers.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as date_type
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import AppConfig
from ..database import get_db
from ..models import FlightRecord

# ---------------------------------------------------------------------------
# Module-level configuration (wired during app startup)
# ---------------------------------------------------------------------------

_templates: Jinja2Templates | None = None
_config: AppConfig | None = None


def configure(templates: Jinja2Templates, config: AppConfig) -> None:
    """Set module-level dependencies. Called once during app startup."""
    global _templates, _config
    _templates = templates
    _config = config


def _get_templates() -> Jinja2Templates:
    if _templates is None:
        raise RuntimeError("Reports router not configured. Call configure() at startup.")
    return _templates


def _get_config() -> AppConfig:
    if _config is None:
        raise RuntimeError("Reports router not configured. Call configure() at startup.")
    return _config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class DaySummary:
    """Statistics for a single day."""

    date: date_type | None
    label: str
    card_count: int = 0
    flight_count: int = 0
    motor_counts: dict[str, int] = field(default_factory=dict)
    total_impulse_ns: float = 0.0
    flyer_stats: dict[str, dict[str, Any]] = field(default_factory=dict)


def _extract_motors(record: FlightRecord) -> list[dict[str, Any]]:
    """Extract the motors list from a record's overflow JSON."""
    if not record.overflow:
        return []
    motors = record.overflow.get("motors")
    if not motors:
        return []
    # Handle legacy nested format
    if motors and isinstance(motors[0], list):
        flat = []
        for stage in motors:
            flat.extend(stage)
        return flat
    return motors


def _motor_total_quantity(motors: list[dict[str, Any]]) -> int:
    """Count total motors including cluster quantities."""
    total = 0
    for m in motors:
        total += m.get("quantity", 1) or 1
    return total


# Standard motor class order: ¼A, ½A, then A through P
_MOTOR_CLASS_ORDER = ["¼A", "½A"] + list("ABCDEFGHIJKLMNOP")


def _motor_class_sort_key(letter: str) -> int:
    """Return a sort index for a motor impulse class letter."""
    try:
        return _MOTOR_CLASS_ORDER.index(letter)
    except ValueError:
        return 99


def _compute_record_impulse(record: FlightRecord, motors: list[dict[str, Any]]) -> float:
    """Compute the impulse for a single record.

    Strategy:
    - If ALL motors have thrustcurve_data with totImpulseNs, calculate
      the sum of (quantity * totImpulseNs) for each motor.
    - If ANY motor is manual (no thrustcurve_id), fall back to the
      record's total_impulse_value (from the card), if present and in Ns.
    - Returns 0.0 if no impulse can be determined.
    """
    if motors:
        all_have_tc = all(
            m.get("thrustcurve_id") and m.get("thrustcurve_data", {}).get("totImpulseNs")
            for m in motors
        )
        if all_have_tc:
            total = 0.0
            for m in motors:
                qty = m.get("quantity", 1) or 1
                total += qty * m["thrustcurve_data"]["totImpulseNs"]
            return total

    # Fall back to card's total_impulse_value
    if record.total_impulse_value and record.total_impulse_unit:
        unit = record.total_impulse_unit.lower().replace(" ", "")
        if unit in ("ns", "n-s", "n·s", "newton-seconds"):
            return record.total_impulse_value

    return 0.0


def _compute_stats(records: list[FlightRecord]) -> dict[str, Any]:
    """Compute aggregate statistics from a list of extracted records.

    Returns a dict with:
      - flight_count: number of flights (1 per card)
      - motor_counts: dict of letter_class -> count
      - total_impulse_ns: sum of total_impulse_value (for Ns units)
      - flyer_stats: dict of flyer_name -> {flights, motors: {letter: count}}
    """
    flight_count = len(records)
    motor_counts: dict[str, int] = defaultdict(int)
    total_impulse_ns = 0.0
    flyer_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"flights": 0, "motors": defaultdict(int), "impulse_ns": 0.0}
    )

    for record in records:
        flyer = record.flier_name or "Unknown"
        flyer_stats[flyer]["flights"] += 1

        # Motor breakdown
        motors = _extract_motors(record)
        for motor in motors:
            letter = motor.get("letter", "?").upper()
            qty = motor.get("quantity", 1) or 1
            motor_counts[letter] += qty
            flyer_stats[flyer]["motors"][letter] += qty

        # Impulse: prefer calculated from ThrustCurve data, fall back to card value
        record_impulse = _compute_record_impulse(record, motors)
        if record_impulse > 0:
            total_impulse_ns += record_impulse
            flyer_stats[flyer]["impulse_ns"] += record_impulse

    # Sort motor counts by letter class order
    motor_counts_sorted = dict(
        sorted(motor_counts.items(), key=lambda x: _motor_class_sort_key(x[0]))
    )

    # Convert flyer_stats defaultdicts to regular dicts and sort by flights
    flyer_stats_sorted = {}
    for name in sorted(flyer_stats.keys(), key=lambda n: flyer_stats[n]["flights"], reverse=True):
        fs = flyer_stats[name]
        motor_total = sum(fs["motors"].values())
        flyer_stats_sorted[name] = {
            "flights": fs["flights"],
            "motor_total": motor_total,
            "motors": dict(sorted(fs["motors"].items(), key=lambda x: _motor_class_sort_key(x[0]))),
            "impulse_ns": fs["impulse_ns"],
        }

    return {
        "flight_count": flight_count,
        "flier_count": len(flyer_stats_sorted),
        "motor_count": sum(motor_counts.values()),
        "motor_counts": motor_counts_sorted,
        "total_impulse_ns": total_impulse_ns,
        "flyer_stats": flyer_stats_sorted,
    }


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/reports")


@router.get("/", response_class=HTMLResponse)
async def reports_overview(
    request: Request,
    day: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Render the overall event reporting page with optional day filter."""
    templates = _get_templates()
    config = _get_config()

    # Parse day filter
    from datetime import timedelta
    filter_date = None
    if day:
        try:
            filter_date = date_type.fromisoformat(day)
        except (ValueError, TypeError):
            pass

    # Fetch all records (for status counts)
    result = await db.execute(
        select(FlightRecord).order_by(FlightRecord.flight_date.asc())
    )
    all_records = list(result.scalars().all())

    # Categorise records and compute status counts
    extracted_records: list[FlightRecord] = []
    status_counts = {
        "pending": 0,
        "processing": 0,
        "extracted": 0,
        "extraction_failed": 0,
    }

    for r in all_records:
        if r.extraction_status in status_counts:
            status_counts[r.extraction_status] += 1
        if r.extraction_status == "extracted":
            extracted_records.append(r)

    # Apply day filter to extracted records for stats
    if filter_date is not None:
        filtered_records = [r for r in extracted_records if r.flight_date == filter_date]
    else:
        filtered_records = extracted_records

    # Collect failed records for display
    failed_records = [
        {"id": r.id, "flier_name": r.flier_name or "Unknown"}
        for r in all_records
        if r.extraction_status == "extraction_failed"
    ]

    # Compute stats from filtered records
    stats = _compute_stats(filtered_records)

    # Build event dates for filter dropdown
    event_dates = []
    current = config.event_date_range.start
    end = config.event_date_range.end
    while current <= end:
        event_dates.append({
            "value": current.isoformat(),
            "label": current.strftime("%A %-m/%-d"),
        })
        current += timedelta(days=1)

    return templates.TemplateResponse(
        name="reports.html",
        request=request,
        context={
            "event_name": config.event_name,
            "total_cards": len(filtered_records),
            "status_counts": status_counts,
            "stats": stats,
            "failed_records": failed_records,
            "event_dates": event_dates,
            "day_filter": day or "",
        },
    )


@router.get("/{report_date}", response_class=HTMLResponse)
async def reports_day(
    request: Request,
    report_date: str,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Render detailed stats for a single day."""
    templates = _get_templates()
    config = _get_config()

    # Parse the date
    try:
        target_date = date_type.fromisoformat(report_date)
    except ValueError:
        return templates.TemplateResponse(
            name="404.html",
            request=request,
            context={
                "event_name": config.event_name,
                "message": f"Invalid date format: {report_date}",
            },
            status_code=404,
        )

    # Fetch records for this date
    result = await db.execute(
        select(FlightRecord).where(
            FlightRecord.flight_date == target_date,
            FlightRecord.extraction_status == "extracted",
        )
    )
    records = list(result.scalars().all())

    if not records:
        return templates.TemplateResponse(
            name="404.html",
            request=request,
            context={
                "event_name": config.event_name,
                "message": f"No extracted records found for {target_date.strftime('%A, %B %d, %Y')}.",
            },
            status_code=404,
        )

    stats = _compute_stats(records)

    return templates.TemplateResponse(
        name="report_day.html",
        request=request,
        context={
            "event_name": config.event_name,
            "date": target_date,
            "date_label": target_date.strftime("%A, %B %d, %Y"),
            "card_count": len(records),
            **stats,
        },
    )
