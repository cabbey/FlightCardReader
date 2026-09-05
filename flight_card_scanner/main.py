"""FastAPI application factory and lifespan context manager.

Handles:
- Loading server configuration from JSON
- Startup checks (static assets)
- Auth database initialization (shared across events)
- EventManager lifecycle (discovery, idle checks, shutdown)
- Mounting static file directories
- Including all routers under event-scoped prefixes
"""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from .config import ServerConfig, load_app_config
from .dependencies.auth import Role, require_role
from .dependencies.event import get_event_db, get_event_info
from .event_manager import EventInfo, EventManager
from .exceptions import ConfigError
from .routers import admin, auth, events, reports, review, scan
from .routers import lost_rockets as lost_rockets_router_mod
from .services.record_service import display_fractions

logger = logging.getLogger(__name__)

# Ensure application log messages are visible on the console.
# Uvicorn configures its own loggers but not the application namespace.
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:     %(message)s",
    stream=sys.stderr,
)

# ---------------------------------------------------------------------------
# Resolve key paths relative to the package directory
# ---------------------------------------------------------------------------

_PACKAGE_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _PACKAGE_DIR / "static"
_TEMPLATES_DIR = _PACKAGE_DIR / "templates"
_OPENCV_JS_DIR = _STATIC_DIR / "js" / "node_modules" / "opencv.js"


# ---------------------------------------------------------------------------
# Startup checks
# ---------------------------------------------------------------------------


def _check_static_assets() -> None:
    """Verify required client-side assets (opencv.js) are present."""
    if not _OPENCV_JS_DIR.exists():
        logger.error(
            "Required client-side asset missing: opencv.js not found at %s. "
            "Run 'pnpm install' in the static/js directory to install dependencies.",
            _OPENCV_JS_DIR,
        )
        sys.exit(1)
    logger.info("Static asset check passed: opencv.js found at %s", _OPENCV_JS_DIR)


# ---------------------------------------------------------------------------
# Background task for idle event checks
# ---------------------------------------------------------------------------


async def _idle_check_loop(event_manager: EventManager, interval_seconds: int = 300):
    """Periodically check for idle events and close them."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await event_manager.check_idle_events()
        except Exception as exc:
            logger.error("Error during idle event check: %s", exc)


# ---------------------------------------------------------------------------
# Lifespan context manager
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup checks, EventManager init, and graceful shutdown."""
    # 1. Load server config
    config_path = Path(os.environ.get("CONFIG_PATH", "config.json"))
    try:
        app_config = load_app_config(config_path)
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    # 1b. Validate FCS_SESSION_SECRET environment variable
    session_secret = os.environ.get("FCS_SESSION_SECRET", "")
    if not session_secret or not session_secret.strip():
        logger.error(
            "FCS_SESSION_SECRET environment variable is required and must be non-empty"
        )
        sys.exit(1)
    if len(session_secret) < 16:
        logger.error(
            "FCS_SESSION_SECRET must be at least 16 characters long"
        )
        sys.exit(1)
    app.state.session_secret = session_secret

    # 1c. Initialize auth database and service (shared across all events)
    from .auth_database import create_auth_tables, init_auth_engine, migrate_auth_columns
    from .services.auth_service import AuthService
    from .middleware.session_middleware import SessionMiddleware as AuthSessionMiddleware

    auth_engine = init_auth_engine(app_config.auth_db_path)
    await create_auth_tables(auth_engine)
    await migrate_auth_columns(auth_engine)

    # 1c2. Initialize lost rockets database (shared across all events)
    from .lost_rockets_database import (
        create_lost_rockets_tables,
        init_lost_rockets_engine,
    )

    lost_rockets_engine = init_lost_rockets_engine(app_config.lost_rockets_db_path)
    await create_lost_rockets_tables(lost_rockets_engine)

    from .auth_database import _auth_session as auth_session_factory
    auth_service = AuthService(
        session_factory=auth_session_factory,
        session_secret=session_secret,
        timeout_hours=app_config.session_timeout_hours,
    )
    app.state.auth_service = auth_service

    # 1d. Initialize audit logger
    from .services.audit_service import init_audit_logger
    # Use a global audit log in the events directory
    audit_log_path = app_config.events_dir / "audit.log"
    init_audit_logger(audit_log_path)

    # 1e. Auto-create default admin if no admin exists
    from sqlalchemy import select as _auth_select
    from .auth_models import User
    async with auth_session_factory() as db:
        result = await db.execute(
            _auth_select(User).where(User.role == "admin")
        )
        admin_exists = result.scalar_one_or_none() is not None

    if not admin_exists:
        admin_email = os.environ.get("FCS_ADMIN_EMAIL", "")
        admin_password = os.environ.get("FCS_ADMIN_PASSWORD", "")
        if admin_email and admin_password:
            await auth_service.create_user(admin_email, "Admin", admin_password, "admin")
            logger.info("Created default admin user: %s", admin_email)
        else:
            logger.warning(
                "No admin user exists and FCS_ADMIN_EMAIL/FCS_ADMIN_PASSWORD not set"
            )

    # 1f. Initialize display name service (email → display name cache)
    from .services.display_name_service import DisplayNameService
    display_name_service = DisplayNameService(auth_session_factory)
    await display_name_service.refresh()
    app.state.display_name_service = display_name_service

    # 2. Check static assets
    _check_static_assets()

    # 3. Create EventManager and discover events
    event_manager = EventManager(app_config)
    event_manager.discover_events()
    await event_manager.gather_all_stats()
    app.state.event_manager = event_manager
    app.state.app_config = app_config

    # 4. Start background task for idle event checks (every 5 minutes)
    idle_task = asyncio.create_task(
        _idle_check_loop(event_manager, interval_seconds=300)
    )

    # 5. Configure templates
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.filters["display_fractions"] = display_fractions
    app.state.templates = templates

    # 6. Configure routers that need template access
    events.configure(templates=templates)
    lost_rockets_router_mod.configure(templates=templates)
    auth.configure(
        auth_service=auth_service,
        templates=templates,
        session_middleware=None,  # Will be set below
    )

    # 6b. Configure session middleware
    session_mw = AuthSessionMiddleware(
        app=None,
        auth_service=auth_service,
        cookie_name="fcs_session",
        session_secret=session_secret,
        secure=bool(app_config.ssl_certfile),
    )
    auth.configure(
        auth_service=auth_service,
        templates=templates,
        session_middleware=session_mw,
    )
    app.state.session_middleware = session_mw

    yield

    # 7. Graceful shutdown
    idle_task.cancel()
    try:
        await idle_task
    except asyncio.CancelledError:
        pass
    await event_manager.shutdown()
    logger.info("Application shut down gracefully.")


# ---------------------------------------------------------------------------
# Application instance
# ---------------------------------------------------------------------------

app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def read_only_guard(request: Request, call_next):
    """Block mutating requests on read-only events with a 403 response."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return await call_next(request)

    # Only check event-scoped routes
    path = request.url.path
    if not path.startswith("/events/"):
        return await call_next(request)

    # Extract the event slug from the URL path
    # Path format: /events/{slug}/...
    # Strip "/events/" prefix and extract the slug (everything before a known route segment)
    remainder = path[len("/events/"):]
    # Known leaf route segments that terminate the slug
    _ROUTE_SEGMENTS = (
        "/api/", "/scan", "/queue", "/admin", "/reports", "/record/", "/images/"
    )
    slug = remainder.rstrip("/")
    for seg in _ROUTE_SEGMENTS:
        idx = remainder.find(seg)
        if idx >= 0:
            slug = remainder[:idx].rstrip("/")
            break

    if not slug:
        return await call_next(request)

    event_manager = getattr(request.app.state, "event_manager", None)
    if event_manager is None:
        return await call_next(request)

    # Check if the event exists and is read-only (without opening it)
    event_info = event_manager.events.get(slug)
    if event_info is not None and event_info.event_config.read_only:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=403,
            content={"detail": "This event is in read-only mode."},
        )

    return await call_next(request)


from .middleware.decoy_middleware import decoy_404_middleware


# NOTE: Registered BEFORE session_resolution on purpose. In Starlette the
# last-added @app.middleware('http') runs OUTERMOST (first on the request), so
# defining session_resolution after this ensures session_resolution wraps the
# decoy middleware and populates request.state.user before the decoy inspects
# the response.
@app.middleware("http")
async def decoy_hostile_404(request: Request, call_next):
    """Replace hostile anonymous 404s with a defensive decoy response."""
    return await decoy_404_middleware(request, call_next)


@app.middleware("http")
async def session_resolution(request: Request, call_next):
    """Resolve session cookie and attach user to request.state."""
    session_mw = getattr(request.app.state, "session_middleware", None)
    if session_mw is None:
        # Middleware not configured yet (during startup)
        request.state.user = None
        request.state.session_token = None
        request.state.clear_session_cookie = False
        return await call_next(request)

    # Use the session middleware's logic to resolve the session
    token = session_mw._get_session_token(request)
    user = None
    clear_cookie = False

    if token:
        client_ip = request.client.host if request.client else None
        user = await session_mw.auth_service.validate_session(token, client_ip=client_ip)
        if user is None:
            clear_cookie = True

    request.state.user = user
    request.state.session_token = token
    request.state.clear_session_cookie = clear_cookie

    response = await call_next(request)

    # If session was invalid, clear the cookie on the response
    if clear_cookie:
        clear_header = session_mw._build_clear_cookie_header()
        response.headers.append("set-cookie", clear_header)

    return response


# Mount static files directory
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# Include top-level routers (events list, auth, lost rockets)
app.include_router(events.router)
app.include_router(auth.router)
app.include_router(lost_rockets_router_mod.router)


# ---------------------------------------------------------------------------
# Event-scoped routes
# ---------------------------------------------------------------------------

event_router = APIRouter(prefix="/events/{event_path:path}")


@event_router.get("/images/{filename:path}")
async def serve_event_image(
    request: Request,
    event_path: str,
    filename: str,
    event_info: EventInfo = Depends(get_event_info),
) -> FileResponse:
    """Serve an image from the event's image store."""
    image_path = event_info.event_config.image_store_path / filename
    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(str(image_path))


# --- Event-scoped review routes ---


@event_router.get("/", response_class=HTMLResponse)
async def event_list_records(
    request: Request,
    event_path: str,
    event_info: EventInfo = Depends(get_event_info),
    db: AsyncSession = Depends(get_event_db),
):
    """Render the flight cards list for an event."""
    from fastapi import Query as Q
    templates = request.app.state.templates
    config = event_info.event_config
    event_base_url = f"/events/{event_path.strip('/')}"

    # Import needed to call the review logic
    return await review.list_records_impl(
        request=request,
        db=db,
        config=config,
        extraction_service=event_info.extraction_service,
        thrustcurve_service=event_info.motor_lookup_service,
        templates=templates,
        event_base_url=event_base_url,
    )


@event_router.get("/queue")
async def event_queue_status(
    request: Request,
    event_path: str,
    event_info: EventInfo = Depends(get_event_info),
    db: AsyncSession = Depends(get_event_db),
):
    """Render the extraction queue page for an event."""
    templates = request.app.state.templates
    config = event_info.event_config
    event_base_url = f"/events/{event_path.strip('/')}"

    return await review.queue_status_impl(
        request=request,
        db=db,
        config=config,
        extraction_service=event_info.extraction_service,
        templates=templates,
        event_base_url=event_base_url,
    )


@event_router.get("/record/{record_id}")
async def event_detail_record(
    request: Request,
    event_path: str,
    record_id: int,
    event_info: EventInfo = Depends(get_event_info),
    db: AsyncSession = Depends(get_event_db),
):
    """Render the detail view for a single flight record."""
    templates = request.app.state.templates
    config = event_info.event_config
    event_base_url = f"/events/{event_path.strip('/')}"

    return await review.detail_record_impl(
        request=request,
        record_id=record_id,
        db=db,
        config=config,
        thrustcurve_service=event_info.motor_lookup_service,
        templates=templates,
        event_base_url=event_base_url,
    )


# --- Event-scoped scan routes ---


@event_router.get("/scan")
async def event_scan_page(
    request: Request,
    event_path: str,
    event_info: EventInfo = Depends(get_event_info),
):
    """Serve the scanner camera UI page for an event."""
    templates = request.app.state.templates
    config = event_info.event_config
    event_base_url = f"/events/{event_path.strip('/')}"

    return await scan.scan_page_impl(
        request=request,
        config=config,
        templates=templates,
        event_base_url=event_base_url,
        app_config=request.app.state.app_config,
    )


@event_router.post(
    "/api/scan",
    status_code=201,
    dependencies=[Depends(require_role(Role.DATA_ENTRY))],
)
async def event_submit_card(
    request: Request,
    event_path: str,
    event_info: EventInfo = Depends(get_event_info),
    db: AsyncSession = Depends(get_event_db),
):
    """Accept a card image upload for an event."""
    from fastapi import File, Form, UploadFile
    # We need to handle form data manually here
    return await scan.submit_card_impl(
        request=request,
        db=db,
        config=event_info.event_config,
        extraction_service=event_info.extraction_service,
    )


# --- Event-scoped reports routes ---


@event_router.get("/reports")
async def event_reports_overview(
    request: Request,
    event_path: str,
    event_info: EventInfo = Depends(get_event_info),
    db: AsyncSession = Depends(get_event_db),
):
    """Render the reports overview for an event."""
    templates = request.app.state.templates
    config = event_info.event_config
    event_base_url = f"/events/{event_path.strip('/')}"

    return await reports.reports_overview_impl(
        request=request,
        db=db,
        config=config,
        templates=templates,
        event_base_url=event_base_url,
    )


@event_router.get("/reports/{report_date}")
async def event_reports_day(
    request: Request,
    event_path: str,
    report_date: str,
    event_info: EventInfo = Depends(get_event_info),
    db: AsyncSession = Depends(get_event_db),
):
    """Render detailed stats for a single day."""
    templates = request.app.state.templates
    config = event_info.event_config
    event_base_url = f"/events/{event_path.strip('/')}"

    return await reports.reports_day_impl(
        request=request,
        report_date=report_date,
        db=db,
        config=config,
        templates=templates,
        event_base_url=event_base_url,
    )


# --- Event-scoped admin routes ---


@event_router.get(
    "/admin",
    dependencies=[Depends(require_role(Role.ADMIN))],
)
async def event_admin_dashboard(
    request: Request,
    event_path: str,
    event_info: EventInfo = Depends(get_event_info),
):
    """Render the admin dashboard for an event."""
    templates = request.app.state.templates
    config = event_info.event_config
    event_base_url = f"/events/{event_path.strip('/')}"

    current_mode = event_info.extraction_service.mode.value if event_info.extraction_service else "unknown"

    return templates.TemplateResponse(
        name="admin.html",
        request=request,
        context={
            "event_name": config.event_name,
            "event_base_url": event_base_url,
            "page_title": f"Admin - {config.event_name}",
            "read_only": config.read_only,
            "current_mode": current_mode,
            "current_user": getattr(request.state, "user", None),
        },
    )


@event_router.post(
    "/api/admin/mode",
    dependencies=[Depends(require_role(Role.DATA_ENTRY))],
)
async def event_set_mode(
    request: Request,
    event_path: str,
    event_info: EventInfo = Depends(get_event_info),
):
    """Switch the extraction operating mode for an event."""
    return await admin.set_mode_impl(
        request=request,
        extraction_service=event_info.extraction_service,
    )


@event_router.post(
    "/api/admin/trigger",
    dependencies=[Depends(require_role(Role.DATA_ENTRY))],
)
async def event_trigger_extraction(
    request: Request,
    event_path: str,
    event_info: EventInfo = Depends(get_event_info),
):
    """Manually trigger extraction of all pending records for an event."""
    return await admin.trigger_extraction_impl(
        request=request,
        extraction_service=event_info.extraction_service,
    )


@event_router.post(
    "/api/admin/requeue",
    dependencies=[Depends(require_role(Role.DATA_ENTRY))],
)
async def event_requeue_all_failed(
    request: Request,
    event_path: str,
    event_info: EventInfo = Depends(get_event_info),
    db: AsyncSession = Depends(get_event_db),
):
    """Reset all extraction_failed records to pending for an event."""
    return await admin.requeue_all_failed_impl(
        request=request,
        db=db,
        extraction_service=event_info.extraction_service,
    )


@event_router.post(
    "/api/admin/requeue/{record_id}",
    dependencies=[Depends(require_role(Role.DATA_ENTRY))],
)
async def event_requeue_single(
    request: Request,
    event_path: str,
    record_id: int,
    event_info: EventInfo = Depends(get_event_info),
    db: AsyncSession = Depends(get_event_db),
):
    """Reset a single failed record to pending for an event."""
    return await admin.requeue_single_impl(
        request=request,
        record_id=record_id,
        db=db,
        extraction_service=event_info.extraction_service,
    )


@event_router.post(
    "/api/admin/extract/{record_id}",
    dependencies=[Depends(require_role(Role.DATA_ENTRY))],
)
async def event_extract_single(
    request: Request,
    event_path: str,
    record_id: int,
    event_info: EventInfo = Depends(get_event_info),
    db: AsyncSession = Depends(get_event_db),
):
    """Force extraction of a single record for an event."""
    return await admin.extract_single_impl(
        request=request,
        record_id=record_id,
        db=db,
        extraction_service=event_info.extraction_service,
    )


@event_router.put(
    "/api/admin/record/{record_id}",
    dependencies=[Depends(require_role(Role.DATA_ENTRY))],
)
async def event_update_record(
    request: Request,
    event_path: str,
    record_id: int,
    event_info: EventInfo = Depends(get_event_info),
    db: AsyncSession = Depends(get_event_db),
):
    """Update editable fields on a flight record for an event."""
    return await admin.update_record_impl(
        request=request,
        record_id=record_id,
        db=db,
        config=event_info.event_config,
        flier_match_service=event_info.flier_match_service,
    )


@event_router.post(
    "/api/admin/record/{record_id}/motor/{motor_index}/search",
    dependencies=[Depends(require_role(Role.DATA_ENTRY))],
)
async def event_search_motor(
    request: Request,
    event_path: str,
    record_id: int,
    motor_index: int,
    event_info: EventInfo = Depends(get_event_info),
    db: AsyncSession = Depends(get_event_db),
):
    """Search ThrustCurve for a motor."""
    return await admin.search_motor_impl(
        request=request,
        record_id=record_id,
        motor_index=motor_index,
        db=db,
        thrustcurve_service=event_info.motor_lookup_service,
    )


@event_router.post(
    "/api/admin/record/{record_id}/motor/{motor_index}/select",
    dependencies=[Depends(require_role(Role.DATA_ENTRY))],
)
async def event_select_motor(
    request: Request,
    event_path: str,
    record_id: int,
    motor_index: int,
    event_info: EventInfo = Depends(get_event_info),
):
    """Validate a motor selection."""
    return await admin.select_motor_impl(
        request=request,
        motor_id=None,  # Will be read from body in the impl
        thrustcurve_service=event_info.motor_lookup_service,
    )


@event_router.get("/api/admin/debug/record/{record_id}")
async def event_debug_record(
    request: Request,
    event_path: str,
    record_id: int,
    event_info: EventInfo = Depends(get_event_info),
    db: AsyncSession = Depends(get_event_db),
):
    """Debug endpoint: dump the raw database record."""
    return await admin.debug_record_impl(record_id=record_id, db=db)


@event_router.get("/api/admin/debug/flier-service")
async def event_debug_flier_service(
    request: Request,
    event_path: str,
    event_info: EventInfo = Depends(get_event_info),
):
    """Debug endpoint: dump flier service state."""
    return admin.debug_flier_service_impl(flier_match_service=event_info.flier_match_service)


@event_router.get("/api/admin/next-unverified")
async def event_next_unverified(
    request: Request,
    event_path: str,
    event_info: EventInfo = Depends(get_event_info),
    db: AsyncSession = Depends(get_event_db),
):
    """Return the ID of the next unverified record."""
    from fastapi import Query as Q
    after = int(request.query_params.get("after", "0"))
    return await admin.next_unverified_impl(after=after, db=db)


@event_router.get("/api/admin/queue")
async def event_get_queue(
    request: Request,
    event_path: str,
    event_info: EventInfo = Depends(get_event_info),
):
    """Return the list of record IDs in the extraction queue."""
    return admin.get_queue_impl(extraction_service=event_info.extraction_service)


@event_router.get("/api/admin/stats")
async def event_get_stats(
    request: Request,
    event_path: str,
    event_info: EventInfo = Depends(get_event_info),
    db: AsyncSession = Depends(get_event_db),
):
    """Return status counts for the event header."""
    return await admin.get_stats_impl(
        db=db,
        extraction_service=event_info.extraction_service,
    )


@event_router.delete(
    "/api/admin/record/{record_id}",
    dependencies=[Depends(require_role(Role.ADMIN))],
)
async def event_delete_record(
    request: Request,
    event_path: str,
    record_id: int,
    event_info: EventInfo = Depends(get_event_info),
    db: AsyncSession = Depends(get_event_db),
):
    """Delete a flight record for an event."""
    return await admin.delete_record_impl(
        request=request,
        record_id=record_id,
        db=db,
    )


# --- Preflight image upload ---


@event_router.post(
    "/api/record/{record_id}/preflight",
    dependencies=[Depends(require_role(Role.FLYER))],
)
async def event_upload_preflight(
    request: Request,
    event_path: str,
    record_id: int,
    event_info: EventInfo = Depends(get_event_info),
    db: AsyncSession = Depends(get_event_db),
):
    """Upload a preflight image for a flight record.

    Requires Role.FLYER minimum. Saves the image and marks overflow
    with preflight_status='pending' and optionally is_lost=True.
    """
    from fastapi import File, Form, UploadFile

    from .services.image_service import save_preflight_image

    # Manually parse the multipart form since we declared deps in the decorator
    form = await request.form()
    preflight_image = form.get("preflight_image")
    mark_lost_raw = form.get("mark_lost", "false")
    mark_lost = mark_lost_raw in ("true", "1", "on", "True")

    if preflight_image is None or not hasattr(preflight_image, "read"):
        raise HTTPException(status_code=400, detail="No preflight image provided")

    # Validate content type
    content_type = getattr(preflight_image, "content_type", "") or ""
    if content_type not in ("image/jpeg", "image/png"):
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Only JPEG and PNG are accepted.",
        )

    # Enforce a 20MB file-size limit. Check Content-Length header first for
    # early rejection, then cap the actual read to prevent memory exhaustion.
    MAX_PREFLIGHT_BYTES = 20 * 1024 * 1024  # 20 MB

    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_PREFLIGHT_BYTES:
        raise HTTPException(
            status_code=413,
            detail="File too large. Maximum upload size is 20 MB.",
        )

    # Read file bytes with a cap
    file_bytes = await preflight_image.read(MAX_PREFLIGHT_BYTES + 1)
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(file_bytes) > MAX_PREFLIGHT_BYTES:
        raise HTTPException(
            status_code=413,
            detail="File too large. Maximum upload size is 20 MB.",
        )

    # Fetch the record
    from sqlalchemy import select as sa_select

    from .models import FlightRecord

    result = await db.execute(
        sa_select(FlightRecord).where(FlightRecord.id == record_id)
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")

    # Check if a preflight image already exists
    from .services.image_service import get_preflight_image_path

    preflight_filename = get_preflight_image_path(record.image_path)
    store_path = event_info.event_config.image_store_path
    if (store_path / preflight_filename).exists():
        raise HTTPException(
            status_code=409, detail="Preflight image already exists for this record"
        )

    # Save the image
    save_preflight_image(record.image_path, file_bytes, store_path)

    # Update overflow
    overflow = dict(record.overflow) if record.overflow else {}
    overflow["preflight_status"] = "pending"

    # Get current user from the require_role dependency (already validated)
    user = getattr(request.state, "user", None)
    overflow["preflight_uploaded_by"] = user.email if user else "unknown"

    if mark_lost:
        overflow["is_lost"] = True

    record.overflow = overflow
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(record, "overflow")
    await db.commit()

    # If marked lost, create a row in the lost rockets database
    if mark_lost:
        from .lost_rockets_database import get_lost_rockets_db
        from .lost_rockets_models import LostRocket

        # Extract display info from overflow
        rocket_colors = overflow.get("rocket_colors") or overflow.get("colors")
        diameter_val = overflow.get("diameter_value", "")
        diameter_unit = overflow.get("diameter_unit", "")
        diameter = f"{diameter_val} {diameter_unit}".strip() if diameter_val else None
        length_val = overflow.get("length_value", "")
        length_unit = overflow.get("length_unit", "")
        length = f"{length_val} {length_unit}".strip() if length_val else None

        # Motor designation from overflow
        motors = overflow.get("motors", [])
        motor_designation = None
        if motors and isinstance(motors, list) and len(motors) > 0:
            first_motor = motors[0]
            if isinstance(first_motor, dict):
                motor_designation = first_motor.get("designation") or first_motor.get("motor_designation")
            elif isinstance(first_motor, str):
                motor_designation = first_motor

        # Get event slug from the URL path
        event_slug = event_path.strip("/")

        # Get event name from the event config
        event_name = event_info.event_config.event_name

        preflight_filename = get_preflight_image_path(record.image_path)

        lost_entry = LostRocket(
            event_slug=event_slug,
            event_name=event_name,
            record_id=record_id,
            flier_name=record.flier_name,
            rocket_colors=rocket_colors if isinstance(rocket_colors, list) else None,
            diameter=diameter,
            length=length,
            motor_designation=motor_designation,
            flight_date=record.flight_date,
            preflight_image_path=preflight_filename,
            added_by=user.email if user else "unknown",
        )

        # Use the lost rockets DB session.
        # Handle IntegrityError gracefully (e.g., retry after partial failure
        # where the lost_rockets row already exists from a previous attempt).
        from sqlalchemy.exc import IntegrityError as SAIntegrityError

        from .lost_rockets_database import _lost_rockets_session
        async with _lost_rockets_session() as lost_db:
            try:
                lost_db.add(lost_entry)
                await lost_db.commit()
            except SAIntegrityError:
                # Row already exists (unique constraint on event_slug + record_id).
                # This is benign - the record was likely created by a prior attempt.
                await lost_db.rollback()

    return {"message": "Preflight image uploaded successfully", "status": "pending"}


@event_router.post(
    "/api/record/{record_id}/clear-lost",
    dependencies=[Depends(require_role(Role.FLYER))],
)
async def event_clear_lost(
    request: Request,
    event_path: str,
    record_id: int,
    event_info: EventInfo = Depends(get_event_info),
    db: AsyncSession = Depends(get_event_db),
):
    """Clear the 'lost' status on a flight record.

    Allowed for the user who set it (by email match) or admin/data_entry roles.
    Keeps the preflight image file intact.

    NOTE: This intentionally does NOT change preflight_status. The preflight image
    remains in moderation (pending or approved) even after the lost flag is cleared.
    Per user requirements: "Clearing lost status: Keep the preflight image file,
    just remove the row from the lost database and clear any flag in the event
    database." The image stays visible in the approval queue for independent review.
    """
    from sqlalchemy import select as sa_select

    from .models import FlightRecord

    result = await db.execute(
        sa_select(FlightRecord).where(FlightRecord.id == record_id)
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")

    overflow = dict(record.overflow) if record.overflow else {}
    if not overflow.get("is_lost"):
        raise HTTPException(status_code=400, detail="Record is not marked as lost")

    # Authorization: only the uploader or admin/data_entry can clear
    user = getattr(request.state, "user", None)
    uploaded_by = overflow.get("preflight_uploaded_by", "")
    user_role = user.role if user else ""
    user_email = user.email if user else ""

    if user_role not in ("admin", "data_entry") and user_email != uploaded_by:
        raise HTTPException(
            status_code=403,
            detail="Only the user who marked it lost or admin/data_entry can clear this",
        )

    overflow["is_lost"] = False
    record.overflow = overflow
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(record, "overflow")
    await db.commit()

    # Remove the corresponding row from lost_rockets database
    event_slug = event_path.strip("/")
    from .lost_rockets_database import _lost_rockets_session
    from .lost_rockets_models import LostRocket
    from sqlalchemy import delete as sa_delete

    if _lost_rockets_session is not None:
        async with _lost_rockets_session() as lost_db:
            await lost_db.execute(
                sa_delete(LostRocket).where(
                    LostRocket.event_slug == event_slug,
                    LostRocket.record_id == record_id,
                )
            )
            await lost_db.commit()

    return {"message": "Lost status cleared"}


app.include_router(event_router)
