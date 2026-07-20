"""Event lifecycle manager for multi-event deployments.

Provides:
- ``EventManager`` -- discovers events from the events directory tree,
  lazily opens/closes database engines and services per event, and closes
  idle events to conserve resources.
- ``EventInfo`` -- dataclass holding per-event runtime state.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import EventConfig, ServerConfig, load_event_config
from .database import Base, create_all
from .models import FlightRecord
from .services.extraction_service import ExtractionService
from .services.flier_match_service import FlierMatchService
from .services.motor_lookup_service import MotorLookupService

logger = logging.getLogger(__name__)


@dataclass
class EventInfo:
    """Runtime state for a single discovered event."""

    slug: str
    config_path: Path
    event_config: EventConfig
    is_open: bool = False
    engine: AsyncEngine | None = None
    session_factory: async_sessionmaker[AsyncSession] | None = None
    extraction_service: ExtractionService | None = None
    motor_lookup_service: MotorLookupService | None = None
    flier_match_service: FlierMatchService | None = None
    last_accessed: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    # Cached card stats (populated at discovery without fully opening the event)
    card_count: int = 0
    verified_pct: float = 0.0


@dataclass
class EventSummary:
    """Lightweight summary for listing events on the top-level page."""

    slug: str
    event_name: str
    event_date_range: Any  # DateRange
    is_open: bool
    card_count: int = 0
    verified_pct: float = 0.0


class EventManager:
    """Discovers and manages the lifecycle of multiple events.

    The manager scans an events directory tree for config.json files,
    lazily opens database connections and services when an event is accessed,
    and closes idle events to free resources.
    """

    def __init__(self, app_config: ServerConfig) -> None:
        self._app_config = app_config
        self._events_dir = app_config.events_dir
        self._idle_timeout_minutes = app_config.event_idle_timeout_minutes
        self._events: dict[str, EventInfo] = {}
        self._lock = asyncio.Lock()

    @property
    def app_config(self) -> ServerConfig:
        """Return the server-level configuration."""
        return self._app_config

    @property
    def events(self) -> dict[str, EventInfo]:
        """Return the internal events dictionary (slug -> EventInfo)."""
        return self._events

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_events(self) -> dict[str, EventInfo]:
        """Walk events_dir recursively for config.json files.

        For each found config.json, compute the event URL slug from its
        relative directory path (e.g. events_dir/2026/nxrs/config.json
        becomes slug '2026/nxrs').

        Returns:
            Dict mapping slug to EventInfo for all discovered events.
        """
        discovered: dict[str, EventInfo] = {}

        if not self._events_dir.exists():
            logger.warning(
                "Events directory does not exist: %s", self._events_dir
            )
            return discovered

        for config_path in sorted(self._events_dir.rglob("config.json")):
            # Compute slug from relative path of the directory containing config.json
            rel_dir = config_path.parent.relative_to(self._events_dir)
            slug = str(rel_dir).replace("\\", "/")

            # Skip if slug would be empty (config.json at root of events_dir)
            if slug == ".":
                slug = ""

            if slug == "":
                logger.warning(
                    "Skipping config.json at events_dir root (%s): "
                    "event configs must be in a subdirectory to provide a URL slug",
                    config_path,
                )
                continue

            try:
                event_config = load_event_config(config_path)
            except Exception as exc:
                logger.error(
                    "Failed to load event config at %s: %s", config_path, exc
                )
                continue

            discovered[slug] = EventInfo(
                slug=slug,
                config_path=config_path,
                event_config=event_config,
            )

        self._events = discovered
        logger.info("Discovered %d event(s) in %s", len(discovered), self._events_dir)
        return discovered

    # ------------------------------------------------------------------
    # Read-only stats gathering
    # ------------------------------------------------------------------

    async def _query_event_stats(self, info: EventInfo) -> None:
        """Query card count and verified % from an event's DB in read-only mode.

        Opens a temporary read-only connection, runs the query, then disposes
        the engine immediately. If the database file does not exist or the
        table is missing (migration needed), stats remain at their defaults
        (0 cards, 0% verified).
        """
        db_path = info.event_config.db_path
        if not db_path.exists():
            logger.debug("No database for event %s; skipping stats.", info.slug)
            return

        # Open in read-only mode so we never trigger a migration
        uri_path = str(db_path).replace("?", "%3f").replace("#", "%23")
        url = f"sqlite+aiosqlite:///file:{uri_path}?mode=ro&uri=true"

        engine = create_async_engine(url, echo=False)
        try:
            async with engine.connect() as conn:
                # Check if the flight_records table exists before querying
                table_exists = await conn.run_sync(
                    lambda sync_conn: sync_conn.dialect.has_table(
                        sync_conn, "flight_records"
                    )
                )
                if not table_exists:
                    logger.debug(
                        "Table flight_records missing for event %s; "
                        "migration will run when event is opened.",
                        info.slug,
                    )
                    return

                # Count total records with extraction_status = 'extracted'
                total_result = await conn.execute(
                    select(func.count()).select_from(FlightRecord)
                )
                total = total_result.scalar() or 0

                # Count verified records (human_verified = true)
                verified_result = await conn.execute(
                    select(func.count())
                    .select_from(FlightRecord)
                    .where(FlightRecord.human_verified == True)  # noqa: E712
                )
                verified = verified_result.scalar() or 0

            info.card_count = total
            info.verified_pct = (verified / total * 100.0) if total > 0 else 0.0
            logger.debug(
                "Event %s stats: %d cards, %.1f%% verified",
                info.slug,
                total,
                info.verified_pct,
            )
        except Exception as exc:
            logger.warning(
                "Failed to query stats for event %s: %s", info.slug, exc
            )
        finally:
            await engine.dispose()

    async def gather_all_stats(self) -> None:
        """Query card stats for all discovered events.

        Called after discover_events() or refresh_events() to populate
        card_count and verified_pct on each EventInfo without fully
        opening the events.
        """
        for slug, info in self._events.items():
            # Skip events that are already open -- their stats will be
            # live from the active database session anyway.
            if info.is_open:
                continue
            await self._query_event_stats(info)

    # ------------------------------------------------------------------
    # Event lifecycle
    # ------------------------------------------------------------------

    async def _open_event_unlocked(self, slug: str) -> EventInfo:
        """Internal: open an event without acquiring the lock.

        Caller must hold self._lock before calling this method.
        """
        if slug not in self._events:
            raise KeyError(f"Event not found: {slug!r}")

        info = self._events[slug]
        if info.is_open:
            info.last_accessed = datetime.now(timezone.utc)
            return info

        event_config = info.event_config

        # Ensure event data directory exists
        event_config.event_data_path.mkdir(parents=True, exist_ok=True)

        # Initialize database engine (per-event, not the module-level singleton)
        db_path = event_config.db_path
        if event_config.read_only:
            uri_path = str(db_path).replace("?", "%3f").replace("#", "%23")
            url = f"sqlite+aiosqlite:///file:{uri_path}?mode=ro&uri=true"
        else:
            url = f"sqlite+aiosqlite:///{db_path}"

        engine = create_async_engine(url, echo=False)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        # Create tables if not read-only
        if not event_config.read_only:
            await create_all(engine)

        # Start motor lookup service
        motor_lookup_service = MotorLookupService()
        await motor_lookup_service.startup()

        # Initialize flier match service if configured
        flier_match_service = None
        if event_config.known_fliers_path:
            flier_match_service = FlierMatchService(
                known_fliers_path=event_config.known_fliers_path,
            )
            flier_match_service.load()

        # Create and start extraction service
        extraction_service = ExtractionService(
            config=event_config,
            session_factory=session_factory,
            thrustcurve_service=motor_lookup_service,
            flier_match_service=flier_match_service,
            extraction_mode=self._app_config.extraction_mode,
            extraction_endpoints=self._app_config.extraction_endpoints,
        )

        if not event_config.read_only:
            await extraction_service.start()

        # Update EventInfo
        info.engine = engine
        info.session_factory = session_factory
        info.extraction_service = extraction_service
        info.motor_lookup_service = motor_lookup_service
        info.flier_match_service = flier_match_service
        info.is_open = True
        info.last_accessed = datetime.now(timezone.utc)

        logger.info("Opened event: %s (%s)", slug, event_config.event_name)
        return info

    async def open_event(self, slug: str) -> EventInfo:
        """Open an event: initialize database engine, create tables, start services.

        Args:
            slug: The event's URL slug (relative path within events_dir).

        Returns:
            The updated EventInfo with is_open=True.

        Raises:
            KeyError: If the slug is not found in discovered events.
        """
        async with self._lock:
            return await self._open_event_unlocked(slug)

    async def _close_event_unlocked(self, slug: str) -> None:
        """Internal: close an event without acquiring the lock.

        Caller must hold self._lock before calling this method.
        """
        if slug not in self._events:
            raise KeyError(f"Event not found: {slug!r}")

        info = self._events[slug]
        if not info.is_open:
            return

        # Stop extraction service
        if info.extraction_service is not None:
            try:
                await info.extraction_service.stop()
            except Exception as exc:
                logger.warning(
                    "Error stopping extraction service for %s: %s", slug, exc
                )

        # Dispose engine
        if info.engine is not None:
            try:
                await info.engine.dispose()
            except Exception as exc:
                logger.warning(
                    "Error disposing engine for %s: %s", slug, exc
                )

        # Clear references
        info.engine = None
        info.session_factory = None
        info.extraction_service = None
        info.motor_lookup_service = None
        info.flier_match_service = None
        info.is_open = False

        logger.info("Closed event: %s", slug)

    async def close_event(self, slug: str) -> None:
        """Close an event: stop services, dispose engine, clear references.

        Args:
            slug: The event's URL slug.

        Raises:
            KeyError: If the slug is not found in discovered events.
        """
        async with self._lock:
            await self._close_event_unlocked(slug)

    async def get_event(self, slug: str) -> EventInfo:
        """Get an event, lazily opening it if not already open.

        Updates last_accessed timestamp on each access.

        Args:
            slug: The event's URL slug.

        Returns:
            The EventInfo (opened if necessary).

        Raises:
            KeyError: If the slug is not found in discovered events.
        """
        async with self._lock:
            if slug not in self._events:
                raise KeyError(f"Event not found: {slug!r}")

            info = self._events[slug]
            if not info.is_open:
                return await self._open_event_unlocked(slug)
            else:
                info.last_accessed = datetime.now(timezone.utc)
                return info

    # ------------------------------------------------------------------
    # Idle management
    # ------------------------------------------------------------------

    async def check_idle_events(self) -> list[str]:
        """Close events that have been idle longer than the timeout.

        Returns:
            List of slugs that were closed due to idle timeout.
        """
        async with self._lock:
            now = datetime.now(timezone.utc)
            closed: list[str] = []

            for slug, info in list(self._events.items()):
                if not info.is_open:
                    continue
                idle_minutes = (now - info.last_accessed).total_seconds() / 60.0
                if idle_minutes >= self._idle_timeout_minutes:
                    await self._close_event_unlocked(slug)
                    closed.append(slug)

            if closed:
                logger.info(
                    "Closed %d idle event(s): %s", len(closed), ", ".join(closed)
                )

            return closed

    # ------------------------------------------------------------------
    # Refresh and listing
    # ------------------------------------------------------------------

    def refresh_events(self) -> dict[str, EventInfo]:
        """Re-discover events without closing already-open ones.

        New events are added, removed events (whose config.json is gone)
        are removed only if they are not currently open. Open events that
        no longer have a config.json on disk remain until closed.

        Returns:
            Updated events dictionary.
        """
        if not self._events_dir.exists():
            logger.warning(
                "Events directory does not exist: %s", self._events_dir
            )
            return self._events

        freshly_discovered: dict[str, EventInfo] = {}

        for config_path in sorted(self._events_dir.rglob("config.json")):
            rel_dir = config_path.parent.relative_to(self._events_dir)
            slug = str(rel_dir).replace("\\", "/")
            if slug == ".":
                slug = ""

            if slug == "":
                logger.warning(
                    "Skipping config.json at events_dir root (%s): "
                    "event configs must be in a subdirectory to provide a URL slug",
                    config_path,
                )
                continue

            # If already known and open, keep the existing EventInfo
            if slug in self._events and self._events[slug].is_open:
                freshly_discovered[slug] = self._events[slug]
                continue

            # If already known but not open, re-load config in case it changed
            try:
                event_config = load_event_config(config_path)
            except Exception as exc:
                logger.error(
                    "Failed to load event config at %s: %s", config_path, exc
                )
                continue

            if slug in self._events:
                # Update config but keep existing EventInfo structure
                existing = self._events[slug]
                existing.event_config = event_config
                existing.config_path = config_path
                freshly_discovered[slug] = existing
            else:
                freshly_discovered[slug] = EventInfo(
                    slug=slug,
                    config_path=config_path,
                    event_config=event_config,
                )

        # Preserve open events that are no longer on disk
        for slug, info in self._events.items():
            if info.is_open and slug not in freshly_discovered:
                freshly_discovered[slug] = info

        self._events = freshly_discovered
        logger.info(
            "Refreshed events: %d event(s) in %s",
            len(freshly_discovered),
            self._events_dir,
        )
        return self._events

    def list_events(self) -> list[EventSummary]:
        """Return a list of event summaries for the top-level events page.

        Returns:
            List of EventSummary objects sorted by slug.
        """
        summaries = []
        for slug in sorted(self._events.keys()):
            info = self._events[slug]
            summaries.append(
                EventSummary(
                    slug=slug,
                    event_name=info.event_config.event_name,
                    event_date_range=info.event_config.event_date_range,
                    is_open=info.is_open,
                    card_count=info.card_count,
                    verified_pct=info.verified_pct,
                )
            )
        return summaries

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Close all open events. Called during graceful application shutdown."""
        async with self._lock:
            open_slugs = [
                slug for slug, info in self._events.items() if info.is_open
            ]
            for slug in open_slugs:
                await self._close_event_unlocked(slug)

        logger.info("EventManager shut down: closed %d event(s)", len(open_slugs))
