"""Tests for the My Flights button on the cards list page.

Verifies that:
- The My Flights button is a simple link that sets q=<display_name>.
- The button appears for any logged-in user who has a display_name.
- Clicking the link (i.e. navigating with ?q=display_name) filters records
  via the existing ILIKE text search.
- The user's email is never exposed in the response.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import quote

import pytest
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from flight_card_scanner.database import Base
from flight_card_scanner.models import FlightRecord
from flight_card_scanner.routers import review


TEMPLATES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "flight_card_scanner",
    "templates",
)


# ---------------------------------------------------------------------------
# Fake user objects for testing
# ---------------------------------------------------------------------------


@dataclass
class FakeUser:
    """Minimal user object simulating the User model for templates."""

    username: str = "testuser"
    email: str = "hidden@secret.com"
    display_name: str = "Test User"
    role: str = "flyer"
    active: bool = True


@dataclass
class FakeDateRange:
    start: date
    end: date


@dataclass
class FakeConfig:
    """Minimal config simulating EventConfig."""

    event_name: str = "Test Event"
    read_only: bool = False
    image_store_path: str = "/tmp/images"
    event_date_range: FakeDateRange = field(
        default_factory=lambda: FakeDateRange(
            start=date.today(), end=date.today()
        )
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_engine(tmp_path):
    """Create a temporary database engine with FlightRecord table."""
    db_path = tmp_path / "test_records.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session_factory(db_engine):
    """Create session factory."""
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture
async def seed_records(db_session_factory):
    """Seed the database with sample flight records."""
    async with db_session_factory() as session:
        records = [
            FlightRecord(
                flier_name="John Smith",
                flier_verified=True,
                extraction_status="extracted",
                image_path="/fake/img1.jpg",
            ),
            FlightRecord(
                flier_name="John Smith",
                flier_verified=True,
                extraction_status="extracted",
                image_path="/fake/img2.jpg",
            ),
            FlightRecord(
                flier_name="John Smith",
                flier_verified=False,
                extraction_status="extracted",
                image_path="/fake/img3.jpg",
            ),
            FlightRecord(
                flier_name="Jane Doe",
                flier_verified=True,
                extraction_status="extracted",
                image_path="/fake/img4.jpg",
            ),
            FlightRecord(
                flier_name="Bob Builder",
                flier_verified=True,
                extraction_status="extracted",
                image_path="/fake/img5.jpg",
            ),
        ]
        for r in records:
            session.add(r)
        await session.commit()


def _build_test_app(user: FakeUser | None, db_session_factory):
    """Build a minimal FastAPI app that calls list_records_impl."""
    app = FastAPI()
    templates = Jinja2Templates(directory=TEMPLATES_DIR)

    @app.middleware("http")
    async def inject_user(request: Request, call_next):
        request.state.user = user
        response = await call_next(request)
        return response

    @app.get("/")
    async def list_cards(request: Request):
        async with db_session_factory() as session:
            return await review.list_records_impl(
                request=request,
                db=session,
                config=FakeConfig(),
                extraction_service=None,
                thrustcurve_service=None,
                templates=templates,
                event_base_url="",
            )

    return app


# ---------------------------------------------------------------------------
# Tests: My Flights button renders as a simple link with q=display_name
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_my_flights_button_links_to_text_search(
    db_session_factory, seed_records
):
    """The My Flights button should be a link that sets q=<display_name>."""
    user = FakeUser(display_name="John Smith")
    app = _build_test_app(user, db_session_factory)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/")

    assert response.status_code == 200
    html = response.text
    # The button should link with q=John+Smith (or q=John%20Smith)
    assert "My Flights" in html
    # The link should contain the display name as a q parameter
    assert "q=John" in html


@pytest.mark.anyio
async def test_my_flights_text_search_filters_records(
    db_session_factory, seed_records
):
    """Navigating with ?q=display_name filters records by flier name."""
    user = FakeUser(display_name="John Smith")
    app = _build_test_app(user, db_session_factory)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/?q=John+Smith")

    assert response.status_code == 200
    html = response.text

    # Should show John Smith's records (all 3 - ILIKE text search)
    assert "John Smith" in html
    # Should NOT show Jane Doe or Bob Builder
    assert "Jane Doe" not in html
    assert "Bob Builder" not in html


@pytest.mark.anyio
async def test_my_flights_button_visible_for_logged_in_user(
    db_session_factory, seed_records
):
    """The My Flights button appears when user is logged in with a display_name."""
    user = FakeUser(display_name="John Smith")
    app = _build_test_app(user, db_session_factory)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "My Flights" in html


@pytest.mark.anyio
async def test_my_flights_button_not_visible_without_display_name(
    db_session_factory, seed_records
):
    """The My Flights button does NOT appear when user has no display_name."""
    user = FakeUser(display_name="")
    app = _build_test_app(user, db_session_factory)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "My Flights" not in html


@pytest.mark.anyio
async def test_my_flights_button_not_visible_no_user(
    db_session_factory, seed_records
):
    """The My Flights button does NOT appear when there is no logged-in user."""
    app = _build_test_app(None, db_session_factory)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "My Flights" not in html


@pytest.mark.anyio
async def test_no_my_flights_parameter_in_urls(
    db_session_factory, seed_records
):
    """The my_flights parameter should no longer exist anywhere in the output."""
    user = FakeUser(display_name="John Smith")
    app = _build_test_app(user, db_session_factory)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "my_flights" not in html


@pytest.mark.anyio
async def test_search_field_shows_display_name_when_q_set(
    db_session_factory, seed_records
):
    """When navigating with ?q=display_name the search field shows the value."""
    user = FakeUser(display_name="John Smith")
    app = _build_test_app(user, db_session_factory)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/?q=John+Smith")

    assert response.status_code == 200
    html = response.text
    # The search input should have the value set
    assert 'value="John Smith"' in html


@pytest.mark.anyio
async def test_my_flights_combined_with_other_filters(
    db_session_factory, seed_records
):
    """Text search works additively with other filters like status."""
    user = FakeUser(display_name="John Smith")
    app = _build_test_app(user, db_session_factory)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/?q=John+Smith&status=extracted")

    assert response.status_code == 200
    html = response.text
    # Should still filter to only John Smith records
    assert "Jane Doe" not in html
    assert "Bob Builder" not in html


@pytest.mark.anyio
async def test_email_not_exposed_in_response(
    db_session_factory, seed_records
):
    """The user's email address NEVER appears in the page HTML, URL, or JS."""
    user = FakeUser(
        email="secret_email@private.com",
        display_name="John Smith",
    )
    app = _build_test_app(user, db_session_factory)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Test without search
        response = await client.get("/")
        assert "secret_email@private.com" not in response.text
        assert "secret_email" not in response.text

        # Test with text search active
        response = await client.get("/?q=John+Smith")
        assert "secret_email@private.com" not in response.text
        assert "secret_email" not in response.text
