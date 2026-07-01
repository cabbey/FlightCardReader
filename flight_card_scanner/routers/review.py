"""Review UI router (GET / and GET /record/{id}).

Serves the paginated list view and single-record detail view using Jinja2
templates.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import AppConfig
from ..database import get_db
from ..models import FlightRecord
from ..services.extraction_service import ExtractionService
from ..services.record_service import motor_designation_str

# ---------------------------------------------------------------------------
# Module-level configuration (wired during app startup)
# ---------------------------------------------------------------------------

_templates: Jinja2Templates | None = None
_config: AppConfig | None = None
_extraction_service: ExtractionService | None = None
_thrustcurve_service = None


def configure(
    templates: Jinja2Templates,
    config: AppConfig,
    extraction_service: ExtractionService,
    thrustcurve_service=None,
) -> None:
    """Set module-level dependencies. Called once during app startup."""
    global _templates, _config, _extraction_service, _thrustcurve_service
    _templates = templates
    _config = config
    _extraction_service = extraction_service
    _thrustcurve_service = thrustcurve_service


def _get_templates() -> Jinja2Templates:
    if _templates is None:
        raise RuntimeError("Review router not configured. Call configure() at startup.")
    return _templates


def _get_config() -> AppConfig:
    if _config is None:
        raise RuntimeError("Review router not configured. Call configure() at startup.")
    return _config


def _get_extraction_service() -> ExtractionService:
    if _extraction_service is None:
        raise RuntimeError("Review router not configured. Call configure() at startup.")
    return _extraction_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Max records per page per requirement 7.2
_MAX_PAGE_SIZE = 25


@dataclass
class RecordRow:
    """Lightweight object passed to the list template for each record row."""

    id: int
    flier_name: str | None
    rocket_name: str | None
    motor_designation: str | None
    flight_date: Any
    extraction_status: str
    created_at: Any
    human_verified: bool = False
    flier_verified: bool = False
    motors_verified: bool = False
    is_queued: bool = False


def _matches_search(record: FlightRecord, q_lower: str) -> bool:
    """Return True if the record matches the search term in overflow fields.

    Checks rocket_name and motor designation from the overflow JSON.
    (flier_name is already filtered via SQL LIKE.)
    """
    overflow = record.overflow
    if overflow:
        rocket_name = overflow.get("rocket_name")
        if rocket_name and q_lower in rocket_name.lower():
            return True
        motor_str = motor_designation_str(overflow)
        if motor_str and q_lower in motor_str.lower():
            return True
    return False


def _build_event_dates(config: "AppConfig") -> list[dict[str, str]]:
    """Build a list of {value, label} dicts for every date in the event range."""
    dates = []
    current = config.event_date_range.start
    end = config.event_date_range.end
    while current <= end:
        dates.append({
            "value": current.isoformat(),
            "label": current.strftime("%A %-m/%-d"),
        })
        current += timedelta(days=1)
    return dates


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def list_records(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    q: str | None = Query(default=None),
    sort: str = Query(default="id_desc"),
    verified: str | None = Query(default=None),
    status: str | None = Query(default=None),
    flight_day: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Render the paginated list view of flight records.

    Supports:
    - q: search filter on flier_name (SQL), rocket_name and motor (overflow)
    - sort: id_desc (default), id_asc, flier_asc, flier_desc
    - verified: all (default), verified, unverified
    - status: extraction_status filter (pending, processing, extracted, extraction_failed)
    - flight_day: ISO date string to filter by flight_date
    """
    templates = _get_templates()
    config = _get_config()
    extraction_service = _get_extraction_service()

    # Cap page_size to 25 per requirement 7.2
    effective_page_size = min(page_size, _MAX_PAGE_SIZE)

    # --- Compute per-status counts ---
    count_stmt = select(
        FlightRecord.extraction_status,
        func.count(FlightRecord.id),
    ).group_by(FlightRecord.extraction_status)
    count_result = await db.execute(count_stmt)
    status_counts = {
        "pending": 0,
        "processing": 0,
        "extracted": 0,
        "extraction_failed": 0,
    }
    for st, count in count_result.all():
        if st in status_counts:
            status_counts[st] = count

    # --- Compute human_verified counts ---
    verified_count_stmt = select(func.count(FlightRecord.id)).where(
        FlightRecord.human_verified == True  # noqa: E712
    )
    verified_count_result = await db.execute(verified_count_stmt)
    verified_count = verified_count_result.scalar() or 0

    total_all_stmt = select(func.count(FlightRecord.id))
    total_all_result = await db.execute(total_all_stmt)
    total_all = total_all_result.scalar() or 0

    verified_percent = round((verified_count / total_all * 100) if total_all > 0 else 0, 1)

    # --- Determine sort order ---
    sort_options = {
        "id_desc": FlightRecord.id.desc(),
        "id_asc": FlightRecord.id.asc(),
        "flier_asc": FlightRecord.flier_name.asc(),
        "flier_desc": FlightRecord.flier_name.desc(),
    }
    order_clause = sort_options.get(sort, FlightRecord.id.desc())

    # --- Determine verified filter ---
    filter_verified = verified  # "all", "verified", "unverified", or None

    # --- Parse flight_day filter ---
    flight_day_date = None
    if flight_day:
        try:
            from datetime import date as _date
            flight_day_date = _date.fromisoformat(flight_day)
        except (ValueError, TypeError):
            pass

    # --- Build base filter conditions ---
    def _apply_filters(stmt):
        """Apply common filters to a statement."""
        if filter_verified == "verified":
            stmt = stmt.where(FlightRecord.human_verified == True)  # noqa: E712
        elif filter_verified == "unverified":
            stmt = stmt.where(FlightRecord.human_verified == False)  # noqa: E712
        if status:
            stmt = stmt.where(FlightRecord.extraction_status == status)
        if flight_day_date:
            stmt = stmt.where(FlightRecord.flight_date == flight_day_date)
        return stmt

    # --- Build query for records ---
    q_stripped = q.strip() if q else None
    search_term = q_stripped if q_stripped else None

    if search_term:
        q_lower = search_term.lower()
        # SQL LIKE on flier_name
        like_pattern = f"%{search_term}%"
        sql_stmt = (
            select(FlightRecord)
            .where(FlightRecord.flier_name.ilike(like_pattern))
            .order_by(order_clause)
        )
        sql_stmt = _apply_filters(sql_stmt)
        sql_result = await db.execute(sql_stmt)
        sql_matches = list(sql_result.scalars().all())
        sql_match_ids = {r.id for r in sql_matches}

        # Python-side scan for overflow matches (rocket_name, motor designation)
        all_stmt = (
            select(FlightRecord)
            .order_by(order_clause)
        )
        all_stmt = _apply_filters(all_stmt)
        all_result = await db.execute(all_stmt)
        all_records = list(all_result.scalars().all())

        # Combine: SQL matches + overflow matches (deduplicated, preserving order)
        combined: list[FlightRecord] = list(sql_matches)
        for record in all_records:
            if record.id not in sql_match_ids and _matches_search(record, q_lower):
                combined.append(record)

        total_records = len(combined)
        total_pages = max(1, math.ceil(total_records / effective_page_size))
        # Paginate the combined results
        start_idx = (page - 1) * effective_page_size
        end_idx = start_idx + effective_page_size
        page_records = combined[start_idx:end_idx]
    else:
        # No search — straight SQL pagination
        count_all_stmt = select(func.count(FlightRecord.id))
        count_all_stmt = _apply_filters(count_all_stmt)
        count_all_result = await db.execute(count_all_stmt)
        total_records = count_all_result.scalar() or 0
        total_pages = max(1, math.ceil(total_records / effective_page_size))

        offset = (page - 1) * effective_page_size
        records_stmt = (
            select(FlightRecord)
            .order_by(order_clause)
            .offset(offset)
            .limit(effective_page_size)
        )
        records_stmt = _apply_filters(records_stmt)
        records_result = await db.execute(records_stmt)
        page_records = list(records_result.scalars().all())

    # --- Get queued IDs for status indicator ---
    queued_ids = extraction_service.queued_ids

    # --- Build template-friendly record rows ---
    records = []
    for r in page_records:
        rocket_name = r.overflow.get("rocket_name") if r.overflow else None
        motor_desig = motor_designation_str(r.overflow)
        # Motors are "verified" if all have a thrustcurve_id
        motors = (r.overflow or {}).get("motors", [])
        motors_verified = bool(motors) and all(m.get("thrustcurve_id") for m in motors)
        records.append(
            RecordRow(
                id=r.id,
                flier_name=r.flier_name,
                rocket_name=rocket_name,
                motor_designation=motor_desig,
                flight_date=r.flight_date,
                extraction_status=r.extraction_status,
                created_at=r.created_at,
                human_verified=r.human_verified,
                flier_verified=r.flier_verified,
                motors_verified=motors_verified,
                is_queued=(r.id in queued_ids),
            )
        )

    current_mode = extraction_service.mode.value

    return templates.TemplateResponse(
        "list.html",
        {
            "request": request,
            "event_name": config.event_name,
            "records": records,
            "status_counts": status_counts,
            "current_mode": current_mode,
            "q": search_term,
            "page": page,
            "total_pages": total_pages,
            "verified_count": verified_count,
            "total_all": total_all,
            "verified_percent": verified_percent,
            "sort": sort,
            "verified_filter": filter_verified or "all",
            "status_filter": status or "",
            "flight_day_filter": flight_day or "",
            "event_dates": _build_event_dates(config),
        },
    )


@router.get("/record/{record_id}", response_class=HTMLResponse)
async def detail_record(
    request: Request,
    record_id: int,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Render the detail view for a single flight record.

    Returns a 404 HTML page if the record is not found.
    """
    templates = _get_templates()
    config = _get_config()

    record = await db.execute(
        select(FlightRecord).where(FlightRecord.id == record_id)
    )
    record_obj = record.scalar_one_or_none()

    if record_obj is None:
        return templates.TemplateResponse(
            "404.html",
            {
                "request": request,
                "event_name": config.event_name,
                "message": f"Flight record #{record_id} does not exist.",
            },
            status_code=404,
        )

    # Determine prev/next record IDs
    # Prev = lower ID, Next = higher ID
    prev_result = await db.execute(
        select(FlightRecord.id)
        .where(FlightRecord.id < record_id)
        .order_by(FlightRecord.id.desc())
        .limit(1)
    )
    prev_id = prev_result.scalar_one_or_none()

    next_result = await db.execute(
        select(FlightRecord.id)
        .where(FlightRecord.id > record_id)
        .order_by(FlightRecord.id.asc())
        .limit(1)
    )
    next_id = next_result.scalar_one_or_none()

    # Build image_url from image_path
    # The image_path is relative (e.g. "uuid.jpg"), served under /images/
    image_url = f"/images/{record_obj.image_path}"

    # Attach image_url to the record object for template use
    record_obj.image_url = image_url  # type: ignore[attr-defined]

    # Load raw LLM JSON from the sidecar .json file (if it exists)
    llm_raw_json = None
    llm_content_json = None
    llm_thinking = None
    llm_content_thinking = None  # thinking embedded in the content field
    json_filename = Path(record_obj.image_path).stem + ".json"
    json_path = config.image_store_path / json_filename
    if json_path.exists():
        try:
            import re as _re

            llm_raw_json = json.loads(json_path.read_text())
            # Extract interpreted parts
            msg = llm_raw_json.get("message", {})

            # Content field — may contain <think> block before the JSON
            content_str = msg.get("content", "")
            if content_str and content_str.strip():
                cleaned = content_str.strip()

                # Extract any <think>...</think> embedded in content
                think_match = _re.search(
                    r"<think>(.*?)</think>", cleaned, flags=_re.DOTALL
                )
                if think_match:
                    llm_content_thinking = think_match.group(1).strip()
                    cleaned = _re.sub(
                        r"<think>.*?</think>", "", cleaned, flags=_re.DOTALL
                    ).strip()
                elif cleaned.startswith("<think>"):
                    # Unclosed think tag — everything before { is thinking
                    json_start = cleaned.find("{")
                    if json_start >= 0:
                        think_part = cleaned[:json_start]
                        # Strip the <think> tag
                        think_part = think_part.replace("<think>", "").strip()
                        if think_part:
                            llm_content_thinking = think_part
                        cleaned = cleaned[json_start:]

                # Parse the remaining content as JSON
                if cleaned:
                    try:
                        llm_content_json = json.loads(cleaned)
                    except json.JSONDecodeError:
                        llm_content_json = cleaned  # fallback to raw string

            # Thinking block (separate field from Ollama)
            thinking_str = msg.get("thinking", "")
            if thinking_str and thinking_str.strip():
                thinking_str = thinking_str.strip()
                if thinking_str.startswith("<think>"):
                    thinking_str = thinking_str[7:]
                if thinking_str.endswith("</think>"):
                    thinking_str = thinking_str[:-8]
                llm_thinking = thinking_str.strip()
        except (OSError, json.JSONDecodeError):
            pass

    # Enrich motors with cached ThrustCurve data for display (not persisted)
    enriched_motors = None
    if _thrustcurve_service and record_obj.overflow and record_obj.overflow.get("motors"):
        enriched_motors = await _thrustcurve_service.enrich_motors_for_display(
            record_obj.overflow["motors"]
        )
        # Also resolve manufacturer names for dropdown pre-selection
        for motor in enriched_motors:
            raw_mfr = motor.get("manufacturer")
            if raw_mfr:
                resolved = _thrustcurve_service.resolve_manufacturer(raw_mfr)
                if resolved:
                    motor["_resolved_manufacturer"] = resolved

    return templates.TemplateResponse(
        "detail.html",
        {
            "request": request,
            "event_name": config.event_name,
            "record": record_obj,
            "prev_id": prev_id,
            "next_id": next_id,
            "llm_raw_json": llm_raw_json,
            "llm_content_json": llm_content_json,
            "llm_thinking": llm_thinking,
            "llm_content_thinking": llm_content_thinking,
            "tc_metadata": _thrustcurve_service._metadata if _thrustcurve_service else None,
            "enriched_motors": enriched_motors,
            "event_dates": _build_event_dates(config),
            "show_all_fields": True,
        },
    )
