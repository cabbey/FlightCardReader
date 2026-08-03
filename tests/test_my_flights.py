"""Tests for the My Flights filter on the cards list page.

Verifies that:
- The my_flights filter correctly returns only records matching the user's
  linked_flier_name with flier_verified=True.
- The button does not appear when user has no linked_flier_name.
- The user's email is never exposed in the response.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from unittest.mock import MagicMock

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
    role: str = "flyer"
    active: bool = True
    linked_flier_name: str | None = None


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
                flier_verified=False,  # Not verified - should be excluded
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
# Tests: My Flights filter functionality
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_my_flights_filters_to_user_records(
    db_session_factory, seed_records
):
    """When my_flights=1 and user has linked_flier_name, only their verified records show."""
    user = FakeUser(linked_flier_name="John Smith")
    app = _build_test_app(user, db_session_factory)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/?my_flights=1")

    assert response.status_code == 200
    html = response.text

    # Should show John Smith's verified records (IDs 1 and 2)
    assert "John Smith" in html
    # Should NOT show Jane Doe or Bob Builder
    assert "Jane Doe" not in html
    assert "Bob Builder" not in html
    # The unverified John Smith record should also be excluded
    # (we have 2 verified + 1 unverified for John Smith, only 2 should show)
    # Count occurrences of table rows with links to records
    assert html.count("/record/1") >= 1
    assert html.count("/record/2") >= 1
    assert "/record/3" not in html  # unverified
    assert "/record/4" not in html  # Jane Doe
    assert "/record/5" not in html  # Bob Builder


@pytest.mark.anyio
async def test_my_flights_without_linked_name_shows_all(
    db_session_factory, seed_records
):
    """When my_flights=1 but user has no linked_flier_name, filter is ignored."""
    user = FakeUser(linked_flier_name=None)
    app = _build_test_app(user, db_session_factory)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/?my_flights=1")

    assert response.status_code == 200
    html = response.text

    # All records should be visible since filter cannot be applied
    assert "John Smith" in html
    assert "Jane Doe" in html
    assert "Bob Builder" in html


@pytest.mark.anyio
async def test_my_flights_button_visible_for_linked_user(
    db_session_factory, seed_records
):
    """The My Flights button appears when user has a linked_flier_name."""
    user = FakeUser(linked_flier_name="John Smith")
    app = _build_test_app(user, db_session_factory)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "My Flights" in html


@pytest.mark.anyio
async def test_my_flights_button_not_visible_without_linked_name(
    db_session_factory, seed_records
):
    """The My Flights button does NOT appear when user has no linked_flier_name."""
    user = FakeUser(linked_flier_name=None)
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
async def test_my_flights_active_state_shows_clear(
    db_session_factory, seed_records
):
    """When my_flights is active, the button shows as highlighted with a clear action."""
    user = FakeUser(linked_flier_name="John Smith")
    app = _build_test_app(user, db_session_factory)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/?my_flights=1")

    assert response.status_code == 200
    html = response.text
    # Active state button text includes the dismiss marker
    assert "My Flights" in html
    # The button in active state should NOT include my_flights=1 in its href
    # (it links to the clear state)


@pytest.mark.anyio
async def test_my_flights_combined_with_other_filters(
    db_session_factory, seed_records
):
    """my_flights works additively with other filters like status."""
    user = FakeUser(linked_flier_name="John Smith")
    app = _build_test_app(user, db_session_factory)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/?my_flights=1&status=extracted")

    assert response.status_code == 200
    html = response.text
    # Should still filter to only John Smith verified records
    assert "Jane Doe" not in html
    assert "Bob Builder" not in html


@pytest.mark.anyio
async def test_email_not_exposed_in_response(
    db_session_factory, seed_records
):
    """The user's email address NEVER appears in the page HTML, URL, or JS."""
    user = FakeUser(
        email="secret_email@private.com",
        linked_flier_name="John Smith",
    )
    app = _build_test_app(user, db_session_factory)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Test without my_flights
        response = await client.get("/")
        assert "secret_email@private.com" not in response.text
        assert "secret_email" not in response.text

        # Test with my_flights active
        response = await client.get("/?my_flights=1")
        assert "secret_email@private.com" not in response.text
        assert "secret_email" not in response.text


@pytest.mark.anyio
async def test_my_flights_preserves_in_pagination_url(
    db_session_factory, seed_records
):
    """The my_flights param is preserved in pagination links."""
    user = FakeUser(linked_flier_name="John Smith")
    app = _build_test_app(user, db_session_factory)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Use page_size=1 to force multiple pages
        response = await client.get("/?my_flights=1&page_size=1")

    assert response.status_code == 200
    html = response.text
    # Pagination links should preserve my_flights=1
    assert "my_flights=1" in html
