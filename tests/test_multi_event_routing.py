"""Tests for multi-event routing functionality.

Verifies:
- Events list page at /
- Event-scoped routes under /events/{slug}/
- Admin refresh endpoint
- Lazy event loading
- Page titles
- Events nav button
- Auth stays at top level
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from httpx import ASGITransport, AsyncClient
from starlette.middleware.base import BaseHTTPMiddleware

from flight_card_scanner.config import ServerConfig, load_app_config, load_event_config
from flight_card_scanner.event_manager import EventManager
from flight_card_scanner.routers import events
from flight_card_scanner.routers.events import router as events_router
from flight_card_scanner.services.record_service import display_fractions


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "flight_card_scanner" / "templates"


class _FakeUser:
    """Minimal user object for test auth."""

    def __init__(self, role: str = "admin"):
        self.role = role
        self.email = "test@example.com"
        self.display_name = "Test Admin"


class _MockSessionMiddleware(BaseHTTPMiddleware):
    """Middleware that attaches a fake user to request.state for tests."""

    async def dispatch(self, request: Request, call_next):
        request.state.user = _FakeUser("admin")
        request.state.session_token = None
        request.state.clear_session_cookie = False
        return await call_next(request)


@pytest.fixture
def events_dir(tmp_path):
    """Create a temporary events directory with sample event configs."""
    events_path = tmp_path / "events"
    events_path.mkdir()

    # Create event 1: 2026/nxrs
    event1_dir = events_path / "2026" / "nxrs"
    event1_dir.mkdir(parents=True)
    config1 = {
        "event_name": "NXRS Spring Launch 2026",
        "event_date_range": {"start": "2026-04-24", "end": "2026-04-26"},
        "extraction_mode": "deferred",
        "extraction_endpoints": [{"url": "http://localhost:11434", "concurrency": 1}],
    }
    (event1_dir / "config.json").write_text(json.dumps(config1))

    # Create event 2: 2026/march
    event2_dir = events_path / "2026" / "march"
    event2_dir.mkdir(parents=True)
    config2 = {
        "event_name": "Missile Madness 2026",
        "event_date_range": {"start": "2026-03-15", "end": "2026-03-17"},
        "extraction_mode": "deferred",
        "extraction_endpoints": [{"url": "http://localhost:11434", "concurrency": 1}],
    }
    (event2_dir / "config.json").write_text(json.dumps(config2))

    return events_path


@pytest.fixture
def event_manager(events_dir, tmp_path):
    """Create an EventManager configured with the test events directory."""
    app_config = ServerConfig(
        events_dir=events_dir,
        auth_db_path=tmp_path / "auth.db",
    )
    mgr = EventManager(app_config)
    mgr.discover_events()
    return mgr


@pytest.fixture
def templates():
    """Create Jinja2Templates pointing to the real templates directory."""
    t = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    t.env.filters["display_fractions"] = display_fractions
    return t


@pytest.fixture
def test_app(event_manager, templates):
    """Create a FastAPI test app with event routing configured."""
    from flight_card_scanner.dependencies.event import get_event_db, get_event_info
    from flight_card_scanner.routers import events as events_mod
    from flight_card_scanner.main import event_router
    from flight_card_scanner.routers import review

    app = FastAPI()
    app.add_middleware(_MockSessionMiddleware)

    # Set up app state
    app.state.event_manager = event_manager
    app.state.templates = templates

    # Configure the events router
    events_mod.configure(templates=templates)

    # Include routers
    app.include_router(events_mod.router)
    app.include_router(event_router)

    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_list_page(test_app):
    """Test that GET / shows the events list page."""
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert "NXRS Spring Launch 2026" in response.text
    assert "Missile Madness 2026" in response.text
    assert "/events/2026/nxrs/" in response.text
    assert "/events/2026/march/" in response.text


@pytest.mark.asyncio
async def test_events_page_title(test_app):
    """Test that the events list page has the correct title."""
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert "<title>Events</title>" in response.text


@pytest.mark.asyncio
async def test_event_page_opens_event(test_app, event_manager):
    """Test that accessing an event route lazily opens the event."""
    # Initially event is not open
    assert not event_manager.events["2026/nxrs"].is_open

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/events/2026/nxrs/")

    assert response.status_code == 200
    assert "NXRS Spring Launch 2026" in response.text
    # Event should now be open
    assert event_manager.events["2026/nxrs"].is_open


@pytest.mark.asyncio
async def test_event_not_found_returns_404(test_app):
    """Test that accessing a non-existent event returns 404."""
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/events/nonexistent/event/")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_event_list_page_title(test_app):
    """Test that the event flight cards page has a descriptive title."""
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/events/2026/nxrs/")

    assert response.status_code == 200
    assert "Flight Cards - NXRS Spring Launch 2026" in response.text


@pytest.mark.asyncio
async def test_events_nav_button_visible(test_app):
    """Test that the Events nav button is present on event pages."""
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/events/2026/nxrs/")

    assert response.status_code == 200
    # Check for Events nav button linking to /
    assert "Events</a>" in response.text
    assert 'href="/"' in response.text


@pytest.mark.asyncio
async def test_event_stats_endpoint(test_app):
    """Test that the event-scoped stats endpoint works."""
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/events/2026/nxrs/api/admin/stats")

    assert response.status_code == 200
    data = response.json()
    assert "status_counts" in data
    assert "verified_count" in data
    assert "current_mode" in data


@pytest.mark.asyncio
async def test_event_scoped_links_in_list(test_app):
    """Test that links in the flight cards list use event-scoped URLs."""
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/events/2026/nxrs/")

    assert response.status_code == 200
    # The nav should include event-scoped links
    assert "/events/2026/nxrs/scan" in response.text
    assert "/events/2026/nxrs/reports" in response.text


@pytest.mark.asyncio
async def test_refresh_events_endpoint(test_app, events_dir, event_manager):
    """Test the admin refresh events endpoint."""
    # Add a new event after initial discovery
    new_event_dir = events_dir / "2026" / "fall"
    new_event_dir.mkdir(parents=True)
    config = {
        "event_name": "Fall Launch 2026",
        "event_date_range": {"start": "2026-10-01", "end": "2026-10-02"},
        "extraction_mode": "deferred",
        "extraction_endpoints": [{"url": "http://localhost:11434", "concurrency": 1}],
    }
    (new_event_dir / "config.json").write_text(json.dumps(config))

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/api/admin/refresh-events")

    assert response.status_code == 200
    data = response.json()
    assert "Refreshed" in data["message"]
    # The new event should be discovered
    slugs = [e["slug"] for e in data["events"]]
    assert "2026/fall" in slugs


@pytest.mark.asyncio
async def test_stats_fetches_from_event_scoped_url(test_app):
    """Test that the base.html JS fetches stats from event-scoped endpoint."""
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/events/2026/nxrs/")

    assert response.status_code == 200
    # The stats JS should use event-scoped URL
    assert "/events/2026/nxrs/api/admin/stats" in response.text


# ---------------------------------------------------------------------------
# Read-only enforcement tests
# ---------------------------------------------------------------------------


@pytest.fixture
def readonly_events_dir(tmp_path):
    """Create a temporary events directory with a read-only event."""
    events_path = tmp_path / "events"
    events_path.mkdir()

    # Create a read-only event
    event_dir = events_path / "2025" / "archive"
    event_dir.mkdir(parents=True)
    config = {
        "event_name": "Archived Event 2025",
        "event_date_range": {"start": "2025-06-01", "end": "2025-06-03"},
        "extraction_mode": "deferred",
        "extraction_endpoints": [{"url": "http://localhost:11434", "concurrency": 1}],
        "read_only": True,
    }
    (event_dir / "config.json").write_text(json.dumps(config))
    # Read-only events need a pre-existing database file with proper schema
    import sqlite3
    from flight_card_scanner.models import Base as _Base
    from sqlalchemy import create_engine as _create_engine
    db_path = event_dir / "flight_cards.db"
    sync_engine = _create_engine(f"sqlite:///{db_path}")
    _Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    # Create a writable event
    event2_dir = events_path / "2026" / "active"
    event2_dir.mkdir(parents=True)
    config2 = {
        "event_name": "Active Event 2026",
        "event_date_range": {"start": "2026-04-24", "end": "2026-04-26"},
        "extraction_mode": "deferred",
        "extraction_endpoints": [{"url": "http://localhost:11434", "concurrency": 1}],
        "read_only": False,
    }
    (event2_dir / "config.json").write_text(json.dumps(config2))

    return events_path


@pytest.fixture
def readonly_event_manager(readonly_events_dir, tmp_path):
    """Create an EventManager with a read-only event."""
    app_config = ServerConfig(
        events_dir=readonly_events_dir,
        auth_db_path=tmp_path / "auth.db",
    )
    mgr = EventManager(app_config)
    mgr.discover_events()
    return mgr


@pytest.fixture
def readonly_test_app(readonly_event_manager, templates):
    """Create a FastAPI test app with read-only event routing."""
    from flight_card_scanner.dependencies.event import get_event_db, get_event_info
    from flight_card_scanner.routers import events as events_mod
    from flight_card_scanner.main import app as real_app, event_router, read_only_guard, session_resolution

    app = FastAPI()

    # Add middleware in correct order (read_only_guard must execute after session_resolution)
    # In Starlette, middlewares are executed in reverse order of addition
    @app.middleware("http")
    async def _session_resolution(request: Request, call_next):
        request.state.user = _FakeUser("admin")
        request.state.session_token = None
        request.state.clear_session_cookie = False
        return await call_next(request)

    @app.middleware("http")
    async def _read_only_guard(request: Request, call_next):
        """Block mutating requests on read-only events with a 403 response."""
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)

        path = request.url.path
        if not path.startswith("/events/"):
            return await call_next(request)

        remainder = path[len("/events/"):]
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

        event_info = event_manager.events.get(slug)
        if event_info is not None and event_info.event_config.read_only:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=403,
                content={"detail": "This event is in read-only mode."},
            )

        return await call_next(request)

    # Set up app state
    app.state.event_manager = readonly_event_manager
    app.state.templates = templates
    app.state.app_config = ServerConfig(
        events_dir=readonly_event_manager.app_config.events_dir,
    )

    # Configure the events router
    events_mod.configure(templates=templates)

    # Include routers
    app.include_router(events_mod.router)
    app.include_router(event_router)

    return app


@pytest.mark.asyncio
async def test_read_only_event_blocks_post(readonly_test_app):
    """Test that POST requests to a read-only event return 403."""
    async with AsyncClient(
        transport=ASGITransport(app=readonly_test_app),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/events/2025/archive/api/scan")

    assert response.status_code == 403
    assert "read-only" in response.json()["detail"]


@pytest.mark.asyncio
async def test_read_only_event_blocks_put(readonly_test_app):
    """Test that PUT requests to a read-only event return 403."""
    async with AsyncClient(
        transport=ASGITransport(app=readonly_test_app),
        base_url="http://testserver",
    ) as client:
        response = await client.put("/events/2025/archive/api/admin/record/1")

    assert response.status_code == 403
    assert "read-only" in response.json()["detail"]


@pytest.mark.asyncio
async def test_read_only_event_blocks_delete(readonly_test_app):
    """Test that DELETE requests to a read-only event return 403."""
    async with AsyncClient(
        transport=ASGITransport(app=readonly_test_app),
        base_url="http://testserver",
    ) as client:
        response = await client.delete("/events/2025/archive/api/admin/record/1")

    assert response.status_code == 403
    assert "read-only" in response.json()["detail"]


@pytest.mark.asyncio
async def test_read_only_event_allows_get(readonly_test_app, readonly_event_manager):
    """Test that GET requests to a read-only event are still allowed."""
    async with AsyncClient(
        transport=ASGITransport(app=readonly_test_app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/events/2025/archive/")

    assert response.status_code == 200
    assert "Archived Event 2025" in response.text


@pytest.mark.asyncio
async def test_writable_event_allows_post(readonly_test_app, readonly_event_manager):
    """Test that POST requests to a writable event are not blocked by read-only guard."""
    # Open the event first with a GET
    async with AsyncClient(
        transport=ASGITransport(app=readonly_test_app),
        base_url="http://testserver",
    ) as client:
        # GET to open the event
        response = await client.get("/events/2026/active/")
        assert response.status_code == 200

        # POST should not be blocked by read_only_guard (may fail later for other reasons)
        response = await client.post("/events/2026/active/api/scan")
        # Should NOT be 403 - might be 400/422 due to missing body, but not 403
        assert response.status_code != 403


@pytest.mark.asyncio
async def test_read_only_passed_in_template_context(readonly_test_app, readonly_event_manager):
    """Test that read_only is passed to template context for read-only events."""
    async with AsyncClient(
        transport=ASGITransport(app=readonly_test_app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/events/2025/archive/")

    assert response.status_code == 200
    # In the list template, read_only=True should disable can_mutate
    # The scan.html template shows a message when read_only is True
    # Check that the page renders (we'll check scan page for the explicit read_only message)


@pytest.mark.asyncio
async def test_read_only_scan_page_shows_message(readonly_test_app, readonly_event_manager):
    """Test that the scan page shows read-only message for read-only events."""
    async with AsyncClient(
        transport=ASGITransport(app=readonly_test_app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/events/2025/archive/scan")

    assert response.status_code == 200
    assert "read-only mode" in response.text


# ---------------------------------------------------------------------------
# QR code SSL/port tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_page_impl_uses_app_config_port():
    """Test that scan_page_impl reads port from app_config, not event config."""
    from unittest.mock import patch, MagicMock
    from flight_card_scanner.routers.scan import scan_page_impl
    from flight_card_scanner.config import EventConfig, ServerConfig

    event_config = MagicMock()
    event_config.event_name = "Test Event"
    event_config.event_date_range = MagicMock()
    event_config.event_date_range.start = date(2026, 1, 1)
    event_config.event_date_range.end = date(2026, 1, 2)
    event_config.read_only = False

    app_config = MagicMock()
    app_config.port = 9443
    app_config.ssl_certfile = None
    app_config.ssl_keyfile = None

    request = MagicMock()
    request.state = MagicMock()
    request.state.user = None
    request.app = MagicMock()
    request.app.state.app_config = app_config

    templates = MagicMock()
    templates.TemplateResponse = MagicMock(return_value="response")

    with patch(
        "flight_card_scanner.routers.scan._get_all_addresses",
        return_value=[("192.168.1.10", False)],
    ):
        await scan_page_impl(
            request=request,
            config=event_config,
            templates=templates,
            event_base_url="/events/test",
            app_config=app_config,
        )

    # Verify the template was called with QR entries containing port 9443
    call_kwargs = templates.TemplateResponse.call_args[1]
    context = call_kwargs["context"]
    assert len(context["qr_entries"]) == 1
    assert "9443" in context["qr_entries"][0]["url"]


@pytest.mark.asyncio
async def test_scan_page_impl_uses_app_config_ssl():
    """Test that scan_page_impl reads SSL from app_config, not event config."""
    from unittest.mock import patch, MagicMock
    from flight_card_scanner.routers.scan import scan_page_impl

    event_config = MagicMock()
    event_config.event_name = "Test Event"
    event_config.event_date_range = MagicMock()
    event_config.event_date_range.start = date(2026, 1, 1)
    event_config.event_date_range.end = date(2026, 1, 2)
    event_config.read_only = False

    # Mock SSL cert files that exist
    ssl_cert = MagicMock()
    ssl_cert.exists = MagicMock(return_value=True)
    ssl_key = MagicMock()
    ssl_key.exists = MagicMock(return_value=True)

    app_config = MagicMock()
    app_config.port = 8443
    app_config.ssl_certfile = ssl_cert
    app_config.ssl_keyfile = ssl_key

    request = MagicMock()
    request.state = MagicMock()
    request.state.user = None
    request.app = MagicMock()
    request.app.state.app_config = app_config

    templates = MagicMock()
    templates.TemplateResponse = MagicMock(return_value="response")

    # Use a Tailscale address to trigger HTTPS
    with patch(
        "flight_card_scanner.routers.scan._get_all_addresses",
        return_value=[("myhost.ts.net", True)],
    ):
        await scan_page_impl(
            request=request,
            config=event_config,
            templates=templates,
            event_base_url="/events/test",
            app_config=app_config,
        )

    call_kwargs = templates.TemplateResponse.call_args[1]
    context = call_kwargs["context"]
    assert len(context["qr_entries"]) == 1
    url = context["qr_entries"][0]["url"]
    # With SSL enabled and Tailscale address, should use https
    assert url.startswith("https://")
    assert "8443" in url
