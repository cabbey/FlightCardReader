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
