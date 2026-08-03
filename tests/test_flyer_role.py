"""Tests for the flyer role and disabled users support.

Verifies:
- Flyer role is correctly positioned in the hierarchy (FLYER < DATA_ENTRY < ADMIN)
- A user with role='flyer' can log in (when active=True)
- A flyer cannot access endpoints that require Role.DATA_ENTRY
- A flyer CAN access endpoints that require Role.FLYER
- Disabled users (active=False) cannot log in
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from flight_card_scanner.auth_models import AuthBase, User
from flight_card_scanner.dependencies.auth import ROLE_MAP, Role, require_role
from flight_card_scanner.middleware.session_middleware import SessionMiddleware
from flight_card_scanner.routers import auth
from flight_card_scanner.routers.auth import router as auth_router
from flight_card_scanner.services.auth_service import AuthService

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSION_SECRET = "flyer-test-secret-at-least-16-chars"
COOKIE_NAME = "fcs_session"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeUser:
    """Minimal user object matching what session middleware provides."""

    id: int = 1
    email: str = "user@test.example"
    display_name: str = "Test User"
    role: str = "flyer"
    active: bool = True


def _make_request(*, user=None, is_api: bool = True):
    """Create a mock Request with the given user and request type."""
    request = MagicMock()
    state = MagicMock()
    state.user = user
    request.state = state

    if is_api:
        request.url.path = "/api/test"
        request.headers.get.return_value = "application/json"
    else:
        request.url.path = "/some-page"
        request.headers.get.return_value = "text/html"

    return request


# ---------------------------------------------------------------------------
# Tests: Role Enum Hierarchy
# ---------------------------------------------------------------------------


class TestRoleEnumHierarchy:
    """Verify that the Role enum values are ordered correctly."""

    def test_role_values(self):
        """Role enum has PUBLIC=0, FLYER=1, DATA_ENTRY=2, ADMIN=3."""
        assert Role.PUBLIC == 0
        assert Role.FLYER == 1
        assert Role.DATA_ENTRY == 2
        assert Role.ADMIN == 3

    def test_flyer_less_than_data_entry(self):
        """FLYER < DATA_ENTRY in the hierarchy."""
        assert Role.FLYER < Role.DATA_ENTRY

    def test_data_entry_less_than_admin(self):
        """DATA_ENTRY < ADMIN in the hierarchy."""
        assert Role.DATA_ENTRY < Role.ADMIN

    def test_flyer_less_than_admin(self):
        """FLYER < ADMIN in the hierarchy."""
        assert Role.FLYER < Role.ADMIN

    def test_flyer_greater_than_public(self):
        """FLYER > PUBLIC in the hierarchy."""
        assert Role.FLYER > Role.PUBLIC

    def test_role_map_includes_flyer(self):
        """ROLE_MAP includes 'flyer': Role.FLYER."""
        assert "flyer" in ROLE_MAP
        assert ROLE_MAP["flyer"] == Role.FLYER

    def test_role_map_all_entries(self):
        """ROLE_MAP has correct entries for all non-PUBLIC roles."""
        assert ROLE_MAP == {
            "admin": Role.ADMIN,
            "data_entry": Role.DATA_ENTRY,
            "flyer": Role.FLYER,
        }


# ---------------------------------------------------------------------------
# Tests: require_role with flyer
# ---------------------------------------------------------------------------


class TestRequireRoleFlyer:
    """Test that require_role() handles the flyer role correctly."""

    @pytest.mark.anyio
    async def test_flyer_can_access_flyer_endpoint(self):
        """A flyer can access endpoints requiring Role.FLYER."""
        user = FakeUser(role="flyer")
        request = _make_request(user=user, is_api=True)
        dependency = require_role(Role.FLYER)
        result = await dependency(request)
        assert result is user

    @pytest.mark.anyio
    async def test_flyer_cannot_access_data_entry_endpoint(self):
        """A flyer cannot access endpoints requiring Role.DATA_ENTRY."""
        user = FakeUser(role="flyer")
        request = _make_request(user=user, is_api=True)
        dependency = require_role(Role.DATA_ENTRY)
        with pytest.raises(HTTPException) as exc_info:
            await dependency(request)
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_flyer_cannot_access_admin_endpoint(self):
        """A flyer cannot access endpoints requiring Role.ADMIN."""
        user = FakeUser(role="flyer")
        request = _make_request(user=user, is_api=True)
        dependency = require_role(Role.ADMIN)
        with pytest.raises(HTTPException) as exc_info:
            await dependency(request)
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_data_entry_can_access_flyer_endpoint(self):
        """A data_entry user can access endpoints requiring Role.FLYER."""
        user = FakeUser(role="data_entry")
        request = _make_request(user=user, is_api=True)
        dependency = require_role(Role.FLYER)
        result = await dependency(request)
        assert result is user

    @pytest.mark.anyio
    async def test_admin_can_access_flyer_endpoint(self):
        """An admin can access endpoints requiring Role.FLYER."""
        user = FakeUser(role="admin")
        request = _make_request(user=user, is_api=True)
        dependency = require_role(Role.FLYER)
        result = await dependency(request)
        assert result is user


# ---------------------------------------------------------------------------
# Tests: Flyer Login (Integration)
# ---------------------------------------------------------------------------


class TestFlyerLogin:
    """Integration tests for flyer login and session behavior."""

    @pytest.fixture
    async def auth_db_session_factory(self):
        """Create an in-memory SQLite async engine and session factory."""
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(AuthBase.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        yield session_factory
        await engine.dispose()

    @pytest.fixture
    async def auth_service(self, auth_db_session_factory):
        """Create a real AuthService with an in-memory database."""
        return AuthService(
            session_factory=auth_db_session_factory,
            session_secret=SESSION_SECRET,
            timeout_hours=8.0,
        )

    @pytest.fixture
    def mock_templates(self):
        """Create mock templates."""
        templates = MagicMock()

        def fake_template_response(*args, **kwargs):
            name = args[0] if args else kwargs.get("name", "unknown.html")
            context = args[1] if len(args) > 1 else kwargs.get("context", {})
            status_code = kwargs.get("status_code", 200)
            error = context.get("error", "")
            body = f"<html><body><h1>{name}</h1>"
            if error:
                body += f'<div class="error">{error}</div>'
            body += "</body></html>"
            return HTMLResponse(content=body, status_code=status_code)

        templates.TemplateResponse = MagicMock(side_effect=fake_template_response)
        return templates

    @pytest.fixture
    async def app_with_auth(self, auth_service, mock_templates):
        """Create a FastAPI app with auth middleware and role-protected endpoints."""
        from fastapi import Depends

        app = FastAPI()

        @app.get("/api/flyer-endpoint")
        async def flyer_endpoint(user=Depends(require_role(Role.FLYER))):
            return JSONResponse(content={"message": "flyer access granted", "role": user.role})

        @app.get("/api/data-entry-endpoint")
        async def data_entry_endpoint(user=Depends(require_role(Role.DATA_ENTRY))):
            return JSONResponse(content={"message": "data_entry access granted", "role": user.role})

        @app.get("/api/admin-endpoint")
        async def admin_endpoint(user=Depends(require_role(Role.ADMIN))):
            return JSONResponse(content={"message": "admin access granted", "role": user.role})

        app.include_router(auth_router)

        session_mw = SessionMiddleware(
            app=app,
            auth_service=auth_service,
            cookie_name=COOKIE_NAME,
            session_secret=SESSION_SECRET,
            secure=False,
        )

        auth.configure(
            auth_service=auth_service,
            templates=mock_templates,
            session_middleware=session_mw,
        )

        yield session_mw

        auth._auth_service = None
        auth._session_middleware = None
        auth._templates = None

    @pytest.fixture
    async def client(self, app_with_auth):
        """Async HTTP client for integration testing."""
        transport = ASGITransport(app=app_with_auth)
        async with AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as ac:
            yield ac

    @pytest.mark.anyio
    async def test_flyer_can_login(self, client, auth_service):
        """A user with role='flyer' and active=True can log in successfully."""
        await auth_service.create_user(
            email="flyer@test.com",
            display_name="Test Flyer",
            password="flyerpassword123",
            role="flyer",
        )

        response = await client.post(
            "/login",
            data={"email": "flyer@test.com", "password": "flyerpassword123"},
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert COOKIE_NAME in response.headers.get("set-cookie", "")

    @pytest.mark.anyio
    async def test_flyer_can_access_flyer_endpoint(self, client, auth_service):
        """A logged-in flyer can access an endpoint requiring Role.FLYER."""
        await auth_service.create_user(
            email="flyer@test.com",
            display_name="Test Flyer",
            password="flyerpassword123",
            role="flyer",
        )

        # Login
        login_response = await client.post(
            "/login",
            data={"email": "flyer@test.com", "password": "flyerpassword123"},
        )
        cookie_value = _extract_cookie_value(login_response.headers.get("set-cookie", ""))

        # Access flyer endpoint
        response = await client.get(
            "/api/flyer-endpoint",
            cookies={COOKIE_NAME: cookie_value},
        )
        assert response.status_code == 200
        assert response.json()["message"] == "flyer access granted"

    @pytest.mark.anyio
    async def test_flyer_cannot_access_data_entry_endpoint(self, client, auth_service):
        """A logged-in flyer cannot access an endpoint requiring Role.DATA_ENTRY."""
        await auth_service.create_user(
            email="flyer@test.com",
            display_name="Test Flyer",
            password="flyerpassword123",
            role="flyer",
        )

        # Login
        login_response = await client.post(
            "/login",
            data={"email": "flyer@test.com", "password": "flyerpassword123"},
        )
        cookie_value = _extract_cookie_value(login_response.headers.get("set-cookie", ""))

        # Access data_entry endpoint
        response = await client.get(
            "/api/data-entry-endpoint",
            cookies={COOKIE_NAME: cookie_value},
        )
        assert response.status_code == 403

    @pytest.mark.anyio
    async def test_flyer_cannot_access_admin_endpoint(self, client, auth_service):
        """A logged-in flyer cannot access an endpoint requiring Role.ADMIN."""
        await auth_service.create_user(
            email="flyer@test.com",
            display_name="Test Flyer",
            password="flyerpassword123",
            role="flyer",
        )

        # Login
        login_response = await client.post(
            "/login",
            data={"email": "flyer@test.com", "password": "flyerpassword123"},
        )
        cookie_value = _extract_cookie_value(login_response.headers.get("set-cookie", ""))

        # Access admin endpoint
        response = await client.get(
            "/api/admin-endpoint",
            cookies={COOKIE_NAME: cookie_value},
        )
        assert response.status_code == 403

    @pytest.mark.anyio
    async def test_disabled_user_cannot_login(self, client, auth_service):
        """A user with active=False cannot log in."""
        user = await auth_service.create_user(
            email="disabled@test.com",
            display_name="Disabled User",
            password="disabledpassword123",
            role="flyer",
        )

        # Disable the user
        from sqlalchemy import update as sql_update

        async with auth_service._session_factory() as db:
            await db.execute(
                sql_update(User).where(User.id == user.id).values(active=False)
            )
            await db.commit()

        # Try to login
        response = await client.post(
            "/login",
            data={"email": "disabled@test.com", "password": "disabledpassword123"},
        )
        assert response.status_code == 401
        assert "Invalid email or password" in response.text

    @pytest.mark.anyio
    async def test_disabled_data_entry_cannot_login(self, client, auth_service):
        """A data_entry user with active=False cannot log in."""
        user = await auth_service.create_user(
            email="deuser@test.com",
            display_name="Disabled DE",
            password="depassword12345",
            role="data_entry",
        )

        # Disable the user
        from sqlalchemy import update as sql_update

        async with auth_service._session_factory() as db:
            await db.execute(
                sql_update(User).where(User.id == user.id).values(active=False)
            )
            await db.commit()

        # Try to login
        response = await client.post(
            "/login",
            data={"email": "deuser@test.com", "password": "depassword12345"},
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_cookie_value(set_cookie_header: str) -> str | None:
    """Extract the cookie value from a Set-Cookie header."""
    if not set_cookie_header:
        return None
    # Format: "fcs_session=VALUE; Path=/; ..."
    parts = set_cookie_header.split(";")
    if parts:
        cookie_part = parts[0].strip()
        if "=" in cookie_part:
            return cookie_part.split("=", 1)[1]
    return None
