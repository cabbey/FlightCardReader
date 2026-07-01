"""FastAPI application factory and lifespan context manager.

Handles:
- Loading configuration from JSON
- Startup checks (image store, database, static assets)
- Mounting static file directories
- Including all routers
- Managing the ExtractionService lifecycle
"""

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import AppConfig, load_config
from .database import create_all, init_engine
from .exceptions import ConfigError
from .routers import admin, reports, review, scan
from .services.extraction_service import ExtractionMode, ExtractionService
from .services.flier_match_service import FlierMatchService
from .services.motor_lookup_service import MotorLookupService

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


def _check_image_store(config: AppConfig) -> None:
    """Verify Image Store directory exists and is writable; create if absent."""
    image_path = config.image_store_path
    if not image_path.exists():
        try:
            image_path.mkdir(parents=True, exist_ok=True)
            logger.info("Created image store directory: %s", image_path)
        except OSError as exc:
            logger.error(
                "Cannot create image store directory %s: %s", image_path, exc
            )
            sys.exit(1)

    if not os.access(image_path, os.W_OK):
        logger.error(
            "Image store directory is not writable: %s", image_path
        )
        sys.exit(1)


async def _check_database(config: AppConfig) -> None:
    """Verify DB file is accessible; init schema if needed."""
    db_path = config.db_path
    # Ensure the parent directory exists
    db_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        engine = init_engine(db_path)
        await create_all(engine)
        logger.info("Database initialised at %s", db_path)
    except Exception as exc:
        logger.error(
            "Cannot initialise database at %s: %s", db_path, exc
        )
        sys.exit(1)


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


def _log_endpoints(config: AppConfig) -> None:
    """Log configured extraction endpoints and their concurrency limits."""
    logger.info(
        "Extraction mode: %s", config.extraction_mode
    )
    for ep in config.extraction_endpoints:
        logger.info(
            "  Endpoint: %s (concurrency: %d)", ep.url, ep.concurrency
        )


def _log_config_summary(config: AppConfig) -> None:
    """Log key configuration values at startup."""
    logger.info("Event: %s (%s to %s)",
                config.event_name,
                config.event_date_range.start,
                config.event_date_range.end)
    logger.info("Event data: %s", config.event_data_path.resolve())
    logger.info("Database: %s", config.db_path.resolve())
    logger.info("Image store: %s", config.image_store_path.resolve())


# ---------------------------------------------------------------------------
# Startup checks orchestrator
# ---------------------------------------------------------------------------


async def startup_checks(config: AppConfig) -> None:
    """Run all startup validation checks."""
    _log_config_summary(config)
    _check_image_store(config)
    await _check_database(config)
    _check_static_assets()
    _log_endpoints(config)


# ---------------------------------------------------------------------------
# Lifespan context manager
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup checks, service init, and graceful shutdown."""
    # 1. Load config
    config_path = Path(os.environ.get("CONFIG_PATH", "config.json"))
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    # 2-4. Run startup checks (image store, DB, static assets, log endpoints)
    await startup_checks(config)

    # 5. Instantiate and start the extraction service
    from .database import _async_session as session_factory
    from .services import record_service

    # 5a. Start motor lookup service
    motor_lookup_service = MotorLookupService()
    await motor_lookup_service.startup()

    # 5b. Initialize FlierMatchService if configured
    flier_match_service = None
    if config.known_fliers_path:
        flier_match_service = FlierMatchService(
            known_fliers_path=config.known_fliers_path,
        )
        flier_match_service.load()

    extraction_service = ExtractionService(
        config=config,
        session_factory=session_factory,
        thrustcurve_service=motor_lookup_service,
        flier_match_service=flier_match_service,
    )

    # Roll back any records stuck in "processing" from a previous unclean shutdown
    async with session_factory() as db:
        stale_records = await record_service.get_by_status(db, "processing")
        for record in stale_records:
            await record_service.set_status(db, record.id, "pending")
        if stale_records:
            logger.info(
                "Rolled back %d stale 'processing' records to 'pending'",
                len(stale_records),
            )

    # Upgrade pending records that already have meaningful extracted data to "extracted"
    async with session_factory() as db:
        pending_records = await record_service.get_by_status(db, "pending")
        upgraded_count = 0
        for record in pending_records:
            # Check if the record has any meaningful data beyond just
            # the image — if so, it was already extracted at some point
            has_flier = bool(record.flier_name)
            has_motors = bool((record.overflow or {}).get("motors"))
            has_rocket = bool((record.overflow or {}).get("rocket_name"))
            has_impulse = record.total_impulse_value is not None
            has_evaluation = bool(record.evaluation_outcome)
            if has_flier or has_motors or has_rocket or has_impulse or has_evaluation:
                await record_service.set_status(db, record.id, "extracted")
                upgraded_count += 1
        if upgraded_count:
            logger.info(
                "Upgraded %d pending records with existing data to 'extracted'",
                upgraded_count,
            )

    await extraction_service.start()
    logger.info("Extraction service started.")

    # In immediate mode, enqueue any pending records (including rolled-back ones)
    if extraction_service.mode == ExtractionMode.IMMEDIATE:
        dispatched = await extraction_service.trigger_pending()
        if dispatched:
            logger.info("Enqueued %d pending records for extraction", dispatched)

    # 6. Configure routers with their dependencies
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    scan.configure(config=config, extraction_service=extraction_service, templates=templates)
    admin.configure(
        extraction_service=extraction_service,
        flier_match_service=flier_match_service,
        config=config,
    )
    review.configure(
        templates=templates, config=config, extraction_service=extraction_service,
        thrustcurve_service=motor_lookup_service,
    )
    reports.configure(templates=templates, config=config)

    # Mount /images now that we know the path and it exists
    app.mount(
        "/images",
        StaticFiles(directory=str(config.image_store_path)),
        name="images",
    )

    # Store config on app state for potential access elsewhere
    app.state.config = config
    app.state.extraction_service = extraction_service
    app.state.thrustcurve_service = motor_lookup_service
    app.state.flier_match_service = flier_match_service

    yield

    # 7. Graceful shutdown: stop extraction service
    await extraction_service.stop()
    logger.info("Extraction service stopped.")


# ---------------------------------------------------------------------------
# Application instance
# ---------------------------------------------------------------------------

app = FastAPI(lifespan=lifespan)

# Mount static files directory
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# Note: /images is mounted dynamically in the lifespan after config is loaded
# and the image store directory has been verified/created.

# Include routers
app.include_router(scan.router)
app.include_router(review.router)
app.include_router(reports.router)
app.include_router(admin.router)
