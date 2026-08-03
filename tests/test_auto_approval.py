"""Tests for auto-approval logic during registration.

Verifies that when a user registers with an email that matches a known flier
in a qualifying event's TSV roster, they are automatically approved as a flyer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from flight_card_scanner.auth_models import AuthBase, User
from flight_card_scanner.routers import auth
from flight_card_scanner.routers.auth import router
from flight_card_scanner.services.auth_service import AuthService
from flight_card_scanner.services.flier_match_service import FlierMatchService


# ---------------------------------------------------------------------------
# Helpers to simulate EventManager and EventInfo
# ---------------------------------------------------------------------------


@dataclass
class FakeDateRange:
    start: date
    end: date


@dataclass
class FakeEventConfig:
    event_name: str = "Test Event"
    known_fliers_path: Path | None = None
    event_date_range: FakeDateRange = field(
        default_factory=lambda: FakeDateRange(
            start=date.today(), end=date.today()
        )
    )


@dataclass
class FakeEventInfo:
    slug: str = "test"
    event_config: FakeEventConfig = field(default_factory=FakeEventConfig)


class FakeEventManager:
    """Minimal event manager mock with an events dict."""

    def __init__(self, events: dict):
        self.events = events


def _create_tsv_file(tmp_path: Path, filename: str, content: str) -> Path:
    """Write TSV content to a file and return the path."""
    tsv_path = tmp_path / filename
    tsv_path.write_text(content, encoding="utf-8")
    return tsv_path


TEMPLATES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "flight_card_scanner",
    "templates",
)


# ---------------------------------------------------------------------------
# Tests: find_by_email on FlierMatchService (synchronous, no fixtures needed)
# ---------------------------------------------------------------------------


class TestFindByEmail:
    """Test the find_by_email method on FlierMatchService."""

    def test_exact_match(self, tmp_path):
        """find_by_email returns the matched row on exact match."""
        tsv = (
            "Name\tEmail\tNAR Number\n"
            "John Smith\tjohn@example.com\t12345\n"
            "Jane Doe\tjane@example.com\t67890\n"
        )
        path = _create_tsv_file(tmp_path, "fliers.tsv", tsv)
        svc = FlierMatchService(known_fliers_path=path)
        svc.load()

        result = svc.find_by_email("john@example.com")
        assert result is not None
        assert result["Name"] == "John Smith"

    def test_case_insensitive(self, tmp_path):
        """find_by_email matches case-insensitively."""
        tsv = (
            "Name\tEmail\tNAR Number\n"
            "John Smith\tJohn@Example.COM\t12345\n"
        )
        path = _create_tsv_file(tmp_path, "fliers.tsv", tsv)
        svc = FlierMatchService(known_fliers_path=path)
        svc.load()

        result = svc.find_by_email("john@example.com")
        assert result is not None
        assert result["Name"] == "John Smith"

    def test_stripped_comparison(self, tmp_path):
        """find_by_email strips whitespace before comparing."""
        tsv = (
            "Name\tEmail\tNAR Number\n"
            "John Smith\t  john@example.com  \t12345\n"
        )
        path = _create_tsv_file(tmp_path, "fliers.tsv", tsv)
        svc = FlierMatchService(known_fliers_path=path)
        svc.load()

        result = svc.find_by_email("john@example.com")
        assert result is not None
        assert result["Name"] == "John Smith"

    def test_no_match(self, tmp_path):
        """find_by_email returns None when email not found."""
        tsv = (
            "Name\tEmail\tNAR Number\n"
            "John Smith\tjohn@example.com\t12345\n"
        )
        path = _create_tsv_file(tmp_path, "fliers.tsv", tsv)
        svc = FlierMatchService(known_fliers_path=path)
        svc.load()

        result = svc.find_by_email("nobody@example.com")
        assert result is None

    def test_empty_email_returns_none(self, tmp_path):
        """find_by_email returns None for empty email input."""
        tsv = (
            "Name\tEmail\tNAR Number\n"
            "John Smith\tjohn@example.com\t12345\n"
        )
        path = _create_tsv_file(tmp_path, "fliers.tsv", tsv)
        svc = FlierMatchService(known_fliers_path=path)
        svc.load()

        result = svc.find_by_email("")
        assert result is None

    def test_service_not_enabled(self, tmp_path):
        """find_by_email returns None when service is not enabled."""
        tsv = "Name\tEmail\tNAR Number\n"
        path = _create_tsv_file(tmp_path, "fliers.tsv", tsv)
        svc = FlierMatchService(known_fliers_path=path)
        svc.load()

        assert svc.enabled is False
        result = svc.find_by_email("john@example.com")
        assert result is None


# ---------------------------------------------------------------------------
# Integration tests: Auto-approval during registration
# ---------------------------------------------------------------------------


@pytest.fixture
async def auth_engine(tmp_path):
    """Create a temporary auth database engine."""
    db_path = tmp_path / "test_auth.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(AuthBase.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def auth_session_factory(auth_engine):
    """Create session factory for the test auth database."""
    return async_sessionmaker(auth_engine, expire_on_commit=False)


@pytest.fixture
async def auth_service(auth_session_factory):
    """Create a real AuthService with the test database."""
    return AuthService(
        session_factory=auth_session_factory,
        session_secret="test-secret",
        timeout_hours=1.0,
    )


@pytest.fixture
def mock_session_middleware():
    """Create a mock SessionMiddleware."""
    mw = MagicMock()
    mw.sign_token = MagicMock(return_value="signed-token-value")
    mw.build_set_cookie_header = MagicMock(
        return_value="fcs_session=signed-token-value; Path=/; HttpOnly; SameSite=Lax"
    )
    mw._build_clear_cookie_header = MagicMock(
        return_value="fcs_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
    )
    return mw


@pytest.fixture
def configure_router(auth_service, mock_session_middleware):
    """Wire up real auth service with Jinja2Templates for the auth router."""
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    auth.configure(
        auth_service=auth_service,
        session_middleware=mock_session_middleware,
        templates=templates,
    )
    yield
    auth._auth_service = None
    auth._session_middleware = None
    auth._templates = None


@pytest.mark.anyio
async def test_auto_approval_matching_email(
    tmp_path, auth_session_factory, configure_router
):
    """User whose email matches a known flier gets auto-approved."""
    tsv_content = (
        "Name\tEmail\tNAR Number\tCertification Level\n"
        "Jane Rocketeer\tjane@rockets.org\t99999\tL2\n"
        "Bob Builder\tbob@example.com\t88888\tL1\n"
    )
    tsv_path = _create_tsv_file(tmp_path, "known_fliers.tsv", tsv_content)

    yesterday = date.today() - timedelta(days=1)
    recent_config = FakeEventConfig(
        event_name="Recent Launch",
        known_fliers_path=tsv_path,
        event_date_range=FakeDateRange(
            start=yesterday - timedelta(days=2),
            end=yesterday,
        ),
    )

    event_manager = FakeEventManager(
        events={
            "2024/recent": FakeEventInfo(slug="2024/recent", event_config=recent_config),
        }
    )

    test_app = FastAPI()
    test_app.include_router(router)
    test_app.state.event_manager = event_manager

    transport = ASGITransport(app=test_app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        response = await client.post(
            "/register",
            data={
                "email": "jane@rockets.org",
                "display_name": "Jane R",
                "password": "StrongPass123!",
                "confirm_password": "StrongPass123!",
                "reason": "I fly rockets",
                "requested_role": "data_entry",
            },
        )
        assert response.status_code == 303

    async with auth_session_factory() as db:
        result = await db.execute(
            select(User).where(User.email == "jane@rockets.org")
        )
        user = result.scalar_one_or_none()
        assert user is not None
        assert user.active is True
        assert user.role == "flyer"


@pytest.mark.anyio
async def test_auto_approval_case_insensitive_email(
    tmp_path, auth_session_factory, configure_router
):
    """Auto-approval email matching is case-insensitive."""
    tsv_content = (
        "Name\tEmail\tNAR Number\tCertification Level\n"
        "Bob Builder\tbob@example.com\t88888\tL1\n"
    )
    tsv_path = _create_tsv_file(tmp_path, "known_fliers.tsv", tsv_content)

    yesterday = date.today() - timedelta(days=1)
    recent_config = FakeEventConfig(
        event_name="Recent Launch",
        known_fliers_path=tsv_path,
        event_date_range=FakeDateRange(
            start=yesterday - timedelta(days=2),
            end=yesterday,
        ),
    )

    event_manager = FakeEventManager(
        events={
            "2024/recent": FakeEventInfo(slug="2024/recent", event_config=recent_config),
        }
    )

    test_app = FastAPI()
    test_app.include_router(router)
    test_app.state.event_manager = event_manager

    transport = ASGITransport(app=test_app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        response = await client.post(
            "/register",
            data={
                "email": "BOB@Example.COM",
                "display_name": "Bob B",
                "password": "StrongPass123!",
                "confirm_password": "StrongPass123!",
                "reason": "I build rockets",
                "requested_role": "flyer",
            },
        )
        assert response.status_code == 303

    async with auth_session_factory() as db:
        result = await db.execute(
            select(User).where(User.email == "bob@example.com")
        )
        user = result.scalar_one_or_none()
        assert user is not None
        assert user.active is True
        assert user.role == "flyer"


@pytest.mark.anyio
async def test_no_auto_approval_for_unmatched_email(
    tmp_path, auth_session_factory, configure_router
):
    """User whose email does not match remains inactive."""
    tsv_content = (
        "Name\tEmail\tNAR Number\n"
        "Jane Rocketeer\tjane@rockets.org\t99999\n"
    )
    tsv_path = _create_tsv_file(tmp_path, "known_fliers.tsv", tsv_content)

    yesterday = date.today() - timedelta(days=1)
    recent_config = FakeEventConfig(
        event_name="Recent Launch",
        known_fliers_path=tsv_path,
        event_date_range=FakeDateRange(
            start=yesterday - timedelta(days=2),
            end=yesterday,
        ),
    )

    event_manager = FakeEventManager(
        events={
            "2024/recent": FakeEventInfo(slug="2024/recent", event_config=recent_config),
        }
    )

    test_app = FastAPI()
    test_app.include_router(router)
    test_app.state.event_manager = event_manager

    transport = ASGITransport(app=test_app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        response = await client.post(
            "/register",
            data={
                "email": "nobody@unknown.org",
                "display_name": "Nobody",
                "password": "StrongPass123!",
                "confirm_password": "StrongPass123!",
                "reason": "Just curious",
                "requested_role": "flyer",
            },
        )
        assert response.status_code == 303

    async with auth_session_factory() as db:
        result = await db.execute(
            select(User).where(User.email == "nobody@unknown.org")
        )
        user = result.scalar_one_or_none()
        assert user is not None
        assert user.active is False
        assert user.role == "flyer"


@pytest.mark.anyio
async def test_only_recent_and_future_events_checked(
    tmp_path, auth_session_factory, configure_router
):
    """Only the most recently completed event and future events are checked."""
    today = date.today()

    # Old event (ended 30 days ago) - should NOT be checked
    old_tsv = (
        "Name\tEmail\tNAR Number\n"
        "Old Flier\told@example.com\t11111\n"
    )
    old_path = _create_tsv_file(tmp_path, "old_fliers.tsv", old_tsv)

    # Recent event (ended 5 days ago) - NOT the most recent completed
    recent_tsv = (
        "Name\tEmail\tNAR Number\n"
        "Recent Flier\trecent@example.com\t22222\n"
    )
    recent_path = _create_tsv_file(tmp_path, "recent_fliers.tsv", recent_tsv)

    # Most recently completed (ended yesterday)
    newest_completed_tsv = (
        "Name\tEmail\tNAR Number\n"
        "Newest Flier\tnewest@example.com\t33333\n"
    )
    newest_path = _create_tsv_file(tmp_path, "newest_fliers.tsv", newest_completed_tsv)

    old_config = FakeEventConfig(
        event_name="Old Launch",
        known_fliers_path=old_path,
        event_date_range=FakeDateRange(
            start=today - timedelta(days=35),
            end=today - timedelta(days=30),
        ),
    )
    recent_config = FakeEventConfig(
        event_name="Recent Launch",
        known_fliers_path=recent_path,
        event_date_range=FakeDateRange(
            start=today - timedelta(days=7),
            end=today - timedelta(days=5),
        ),
    )
    newest_config = FakeEventConfig(
        event_name="Newest Completed Launch",
        known_fliers_path=newest_path,
        event_date_range=FakeDateRange(
            start=today - timedelta(days=3),
            end=today - timedelta(days=1),
        ),
    )

    event_manager = FakeEventManager(
        events={
            "2024/old": FakeEventInfo(slug="2024/old", event_config=old_config),
            "2024/recent": FakeEventInfo(slug="2024/recent", event_config=recent_config),
            "2024/newest": FakeEventInfo(slug="2024/newest", event_config=newest_config),
        }
    )

    test_app = FastAPI()
    test_app.include_router(router)
    test_app.state.event_manager = event_manager

    transport = ASGITransport(app=test_app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        # "old@example.com" should NOT be auto-approved (old event is not most recent)
        response = await client.post(
            "/register",
            data={
                "email": "old@example.com",
                "display_name": "Old Person",
                "password": "StrongPass123!",
                "confirm_password": "StrongPass123!",
                "reason": "Testing",
                "requested_role": "flyer",
            },
        )
        assert response.status_code == 303

    async with auth_session_factory() as db:
        result = await db.execute(
            select(User).where(User.email == "old@example.com")
        )
        user = result.scalar_one_or_none()
        assert user is not None
        assert user.active is False

    # "newest@example.com" SHOULD be auto-approved (most recent completed)
    transport2 = ASGITransport(app=test_app)
    async with AsyncClient(
        transport=transport2, base_url="http://test", follow_redirects=False
    ) as client:
        response = await client.post(
            "/register",
            data={
                "email": "newest@example.com",
                "display_name": "Newest Person",
                "password": "StrongPass123!",
                "confirm_password": "StrongPass123!",
                "reason": "Testing",
                "requested_role": "flyer",
            },
        )
        assert response.status_code == 303

    async with auth_session_factory() as db:
        result = await db.execute(
            select(User).where(User.email == "newest@example.com")
        )
        user = result.scalar_one_or_none()
        assert user is not None
        assert user.active is True
        assert user.role == "flyer"

    # "recent@example.com" should NOT be auto-approved (not the most recent completed)
    transport3 = ASGITransport(app=test_app)
    async with AsyncClient(
        transport=transport3, base_url="http://test", follow_redirects=False
    ) as client:
        response = await client.post(
            "/register",
            data={
                "email": "recent@example.com",
                "display_name": "Recent Person",
                "password": "StrongPass123!",
                "confirm_password": "StrongPass123!",
                "reason": "Testing",
                "requested_role": "flyer",
            },
        )
        assert response.status_code == 303

    async with auth_session_factory() as db:
        result = await db.execute(
            select(User).where(User.email == "recent@example.com")
        )
        user = result.scalar_one_or_none()
        assert user is not None
        assert user.active is False


@pytest.mark.anyio
async def test_future_event_is_checked(
    tmp_path, auth_session_factory, configure_router
):
    """Future events (end >= today) are checked for auto-approval."""
    today = date.today()

    future_tsv = (
        "Name\tEmail\tNAR Number\n"
        "Future Flier\tfuture@example.com\t44444\n"
    )
    future_path = _create_tsv_file(tmp_path, "future_fliers.tsv", future_tsv)

    future_config = FakeEventConfig(
        event_name="Future Launch",
        known_fliers_path=future_path,
        event_date_range=FakeDateRange(
            start=today + timedelta(days=10),
            end=today + timedelta(days=12),
        ),
    )

    event_manager = FakeEventManager(
        events={
            "2025/future": FakeEventInfo(slug="2025/future", event_config=future_config),
        }
    )

    test_app = FastAPI()
    test_app.include_router(router)
    test_app.state.event_manager = event_manager

    transport = ASGITransport(app=test_app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        response = await client.post(
            "/register",
            data={
                "email": "future@example.com",
                "display_name": "Future Person",
                "password": "StrongPass123!",
                "confirm_password": "StrongPass123!",
                "reason": "Flying next month",
                "requested_role": "flyer",
            },
        )
        assert response.status_code == 303

    async with auth_session_factory() as db:
        result = await db.execute(
            select(User).where(User.email == "future@example.com")
        )
        user = result.scalar_one_or_none()
        assert user is not None
        assert user.active is True
        assert user.role == "flyer"


@pytest.mark.anyio
async def test_no_event_manager_skips_auto_approval(
    auth_session_factory, configure_router
):
    """When event_manager is not on app.state, auto-approval is skipped."""
    test_app = FastAPI()
    test_app.include_router(router)
    # Intentionally do NOT set test_app.state.event_manager

    transport = ASGITransport(app=test_app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        response = await client.post(
            "/register",
            data={
                "email": "skipped@example.com",
                "display_name": "Skipped",
                "password": "StrongPass123!",
                "confirm_password": "StrongPass123!",
                "reason": "Testing",
                "requested_role": "flyer",
            },
        )
        assert response.status_code == 303

    async with auth_session_factory() as db:
        result = await db.execute(
            select(User).where(User.email == "skipped@example.com")
        )
        user = result.scalar_one_or_none()
        assert user is not None
        assert user.active is False
