"""Tests for the self-registration flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from flight_card_scanner.auth_models import AuthBase, User
from flight_card_scanner.routers import auth
from flight_card_scanner.routers.auth import router
from flight_card_scanner.services.auth_service import AuthService


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
    factory = async_sessionmaker(auth_engine, expire_on_commit=False)
    return factory


@pytest.fixture
async def auth_service(auth_session_factory):
    """Create a real AuthService with the test database."""
    svc = AuthService(
        session_factory=auth_session_factory,
        session_secret="test-secret",
        timeout_hours=1.0,
    )
    return svc


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


@pytest.fixture(autouse=True)
def configure_auth_router(auth_service, mock_session_middleware):
    """Wire up real auth service with Jinja2Templates for the auth router."""
    from fastapi.templating import Jinja2Templates
    import os

    templates_dir = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "flight_card_scanner",
        "templates",
    )
    templates = Jinja2Templates(directory=templates_dir)

    auth.configure(
        auth_service=auth_service,
        session_middleware=mock_session_middleware,
        templates=templates,
    )
    yield
    # Reset module state
    auth._auth_service = None
    auth._session_middleware = None
    auth._templates = None


@pytest.fixture
def app():
    """Create a FastAPI test app with the auth router."""
    test_app = FastAPI()
    test_app.include_router(router)
    return test_app


@pytest.fixture
async def client(app):
    """Async HTTP client for testing."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# GET /register
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_register_page_returns_200(client):
    """GET /register should return 200 with the registration form."""
    response = await client.get("/register")
    assert response.status_code == 200
    assert "Request Access" in response.text
    assert "Registrations are processed manually" in response.text


# ---------------------------------------------------------------------------
# POST /register - successful registration
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_register_success_creates_inactive_user(
    client, auth_service, auth_session_factory
):
    """POST /register with valid data creates an inactive user with correct role."""
    response = await client.post(
        "/register",
        data={
            "email": "newuser@example.com",
            "display_name": "New User",
            "password": "StrongPass123!",
            "confirm_password": "StrongPass123!",
            "reason": "I want to help track rockets",
            "requested_role": "flyer",
        },
    )
    # Should redirect to login
    assert response.status_code == 303
    assert "/login" in response.headers["location"]
    assert "registered=1" in response.headers["location"]

    # Verify user was created in DB
    async with auth_session_factory() as db:
        result = await db.execute(
            select(User).where(User.email == "newuser@example.com")
        )
        user = result.scalar_one_or_none()
        assert user is not None
        assert user.active is False
        assert user.role == "flyer"
        assert user.reason == "I want to help track rockets"
        assert user.display_name == "New User"


@pytest.mark.anyio
async def test_register_data_entry_role(client, auth_session_factory):
    """POST /register with data_entry role creates user with that role."""
    response = await client.post(
        "/register",
        data={
            "email": "dataentry@example.com",
            "display_name": "Data Person",
            "password": "AnotherGood1!Pass",
            "confirm_password": "AnotherGood1!Pass",
            "reason": "I want to enter data",
            "requested_role": "data_entry",
        },
    )
    assert response.status_code == 303

    async with auth_session_factory() as db:
        result = await db.execute(
            select(User).where(User.email == "dataentry@example.com")
        )
        user = result.scalar_one_or_none()
        assert user is not None
        assert user.role == "data_entry"
        assert user.active is False


# ---------------------------------------------------------------------------
# POST /register - validation failures
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_register_weak_password_too_short(client):
    """POST /register with a short password returns an error."""
    response = await client.post(
        "/register",
        data={
            "email": "user@example.com",
            "display_name": "User",
            "password": "Short1!",
            "confirm_password": "Short1!",
            "reason": "Testing",
            "requested_role": "flyer",
        },
    )
    assert response.status_code == 400
    assert "12 characters" in response.text


@pytest.mark.anyio
async def test_register_weak_password_no_uppercase(client):
    """POST /register with password missing uppercase returns an error."""
    response = await client.post(
        "/register",
        data={
            "email": "user@example.com",
            "display_name": "User",
            "password": "alllowercase1!",
            "confirm_password": "alllowercase1!",
            "reason": "Testing",
            "requested_role": "flyer",
        },
    )
    assert response.status_code == 400
    assert "uppercase" in response.text


@pytest.mark.anyio
async def test_register_weak_password_no_digit_or_special(client):
    """POST /register with password missing digit/special returns an error."""
    response = await client.post(
        "/register",
        data={
            "email": "user@example.com",
            "display_name": "User",
            "password": "NoDigitOrSpecialAa",
            "confirm_password": "NoDigitOrSpecialAa",
            "reason": "Testing",
            "requested_role": "flyer",
        },
    )
    assert response.status_code == 400
    assert "digit or special" in response.text


@pytest.mark.anyio
async def test_register_password_mismatch(client):
    """POST /register with mismatched passwords returns an error."""
    response = await client.post(
        "/register",
        data={
            "email": "user@example.com",
            "display_name": "User",
            "password": "StrongPass123!",
            "confirm_password": "DifferentPass123!",
            "reason": "Testing",
            "requested_role": "flyer",
        },
    )
    assert response.status_code == 400
    assert "do not match" in response.text


@pytest.mark.anyio
async def test_register_duplicate_email(client, auth_session_factory):
    """POST /register with an existing email returns an error."""
    # First registration
    await client.post(
        "/register",
        data={
            "email": "dupe@example.com",
            "display_name": "First",
            "password": "StrongPass123!",
            "confirm_password": "StrongPass123!",
            "reason": "First time",
            "requested_role": "flyer",
        },
    )

    # Second registration with same email
    response = await client.post(
        "/register",
        data={
            "email": "dupe@example.com",
            "display_name": "Second",
            "password": "StrongPass456!",
            "confirm_password": "StrongPass456!",
            "reason": "Second time",
            "requested_role": "data_entry",
        },
    )
    assert response.status_code == 400
    assert "already exists" in response.text


@pytest.mark.anyio
async def test_register_empty_name_rejected(client):
    """POST /register with empty name returns an error."""
    response = await client.post(
        "/register",
        data={
            "email": "user@example.com",
            "display_name": "",
            "password": "StrongPass123!",
            "confirm_password": "StrongPass123!",
            "reason": "Testing",
            "requested_role": "flyer",
        },
    )
    assert response.status_code == 400
    assert "Name is required" in response.text


@pytest.mark.anyio
async def test_register_empty_reason_rejected(client):
    """POST /register with empty reason returns an error."""
    response = await client.post(
        "/register",
        data={
            "email": "user@example.com",
            "display_name": "User",
            "password": "StrongPass123!",
            "confirm_password": "StrongPass123!",
            "reason": "",
            "requested_role": "flyer",
        },
    )
    assert response.status_code == 400
    assert "Reason" in response.text


@pytest.mark.anyio
async def test_register_invalid_email_rejected(client):
    """POST /register with invalid email returns an error."""
    response = await client.post(
        "/register",
        data={
            "email": "notanemail",
            "display_name": "User",
            "password": "StrongPass123!",
            "confirm_password": "StrongPass123!",
            "reason": "Testing",
            "requested_role": "flyer",
        },
    )
    assert response.status_code == 400
    assert "email" in response.text.lower()


# ---------------------------------------------------------------------------
# Inactive user cannot log in
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_registered_user_cannot_login_until_activated(
    client, auth_session_factory
):
    """A newly registered user (active=False) cannot log in."""
    # Register
    await client.post(
        "/register",
        data={
            "email": "pending@example.com",
            "display_name": "Pending",
            "password": "StrongPass123!",
            "confirm_password": "StrongPass123!",
            "reason": "Want access",
            "requested_role": "flyer",
        },
    )

    # Attempt login
    response = await client.post(
        "/login",
        data={"email": "pending@example.com", "password": "StrongPass123!"},
    )
    # Should fail - user is inactive
    assert response.status_code == 401
    assert "Invalid email or password" in response.text
