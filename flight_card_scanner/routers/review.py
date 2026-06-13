"""Review UI router (GET / and GET /record/{id}).

Serves the paginated list view and single-record detail view using Jinja2
templates.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
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


def configure(
    templates: Jinja2Templates,
    config: AppConfig,
    extraction_service: ExtractionService,
) -> None:
    """Set module-level dependencies. Called once during app startup."""
    global _templates, _config, _extraction_service
    _templates = templates
    _config = config
    _extraction_service = extraction_service


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
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Render the paginated list view of flight records.

    Supports optional search (q parameter) that filters on flier_name (SQL),
    rocket_name and motor designation (Python-side overflow scan).
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
    for status, count in count_result.all():
        if status in status_counts:
            status_counts[status] = count

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
            .order_by(FlightRecord.created_at.desc())
        )
        sql_result = await db.execute(sql_stmt)
        sql_matches = list(sql_result.scalars().all())
        sql_match_ids = {r.id for r in sql_matches}

        # Python-side scan for overflow matches (rocket_name, motor designation)
        all_stmt = (
            select(FlightRecord)
            .order_by(FlightRecord.created_at.desc())
        )
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
        count_all_result = await db.execute(count_all_stmt)
        total_records = count_all_result.scalar() or 0
        total_pages = max(1, math.ceil(total_records / effective_page_size))

        offset = (page - 1) * effective_page_size
        records_stmt = (
            select(FlightRecord)
            .order_by(FlightRecord.created_at.desc())
            .offset(offset)
            .limit(effective_page_size)
        )
        records_result = await db.execute(records_stmt)
        page_records = list(records_result.scalars().all())

    # --- Build template-friendly record rows ---
    records = []
    for r in page_records:
        rocket_name = r.overflow.get("rocket_name") if r.overflow else None
        motor_desig = motor_designation_str(r.overflow)
        records.append(
            RecordRow(
                id=r.id,
                flier_name=r.flier_name,
                rocket_name=rocket_name,
                motor_designation=motor_desig,
                flight_date=r.flight_date,
                extraction_status=r.extraction_status,
                created_at=r.created_at,
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
    json_filename = Path(record_obj.image_path).stem + ".json"
    json_path = config.image_store_path / json_filename
    if json_path.exists():
        try:
            llm_raw_json = json.loads(json_path.read_text())
            # Extract interpreted parts
            msg = llm_raw_json.get("message", {})
            # Content is the structured JSON output
            content_str = msg.get("content", "")
            if content_str and content_str.strip():
                try:
                    llm_content_json = json.loads(content_str)
                except json.JSONDecodeError:
                    llm_content_json = content_str  # fallback to raw string
            # Thinking block
            thinking_str = msg.get("thinking", "")
            if thinking_str and thinking_str.strip():
                # Strip <think> tags if present
                thinking_str = thinking_str.strip()
                if thinking_str.startswith("<think>"):
                    thinking_str = thinking_str[7:]
                if thinking_str.endswith("</think>"):
                    thinking_str = thinking_str[:-8]
                llm_thinking = thinking_str.strip()
        except (OSError, json.JSONDecodeError):
            pass

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
        },
    )
