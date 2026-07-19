"""Unit tests for user management API endpoints (task 7.2).

Tests cover:
- GET /api/admin/users (list users, admin only)
- POST /api/admin/users (create user, admin only)
- PUT /api/admin/users/{user_id} (update user, admin only)
- Self-demotion/self-deactivation rejection
- Session invalidation on deactivation
- Duplicate email (409), user not found (404)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from flight_card_scanner.auth_models import AuthBase, User
from flight_card_scanner.routers import auth
from flight_card_scanner.routers.auth import router
from flight_card_scanner.services.auth_service import AuthService


@pytest.fixture
async def auth_engine():
    """Create an in-memory SQLite engine for auth tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(AuthBase.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def auth_session_factory(auth_engine):
    """Create an async session factory for the test auth database."""
    return async_sessionmaker(auth_engine, expire_on_commit=False)


@pytest.fixture
def auth_service(auth_session_factory):
    """Create a real AuthService with an in-memory database."""
    return AuthService(
        session_factory=auth_session_factory,
        session_secret="test-secret-1234567890",
        timeout_hours=8.0,
    )


@pytest.fixture
def mock_templates():
    """Create a mock Jinja2Templates for user management page rendering."""
    from fastapi.responses import HTMLResponse

    templates = MagicMock()

    def fake_template_response(**kwargs):
        status_code = kwargs.get("status_code", 200)
        return HTMLResponse(content="<html><body>Users</body></html>", status_code=status_code)

    templates.TemplateResponse = MagicMock(side_effect=fake_template_response)
    return templates


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
def admin_user():
    """Create a mock admin user for request.state."""
    user = MagicMock()
    user.id = 1
    user.email = "admin@example.com"
    user.display_name = "Admin User"
    user.role = "admin"
    user.active = True
    return user


@pytest.fixture(autouse=True)
def configure_auth_router(auth_service, mock_templates, mock_session_middleware):
    """Wire up real auth service for user management tests."""
    auth.configure(
        auth_service=auth_service,
        templates=mock_templates,
        session_middleware=mock_session_middleware,
    )
    yield
    auth._auth_service = None
    auth._session_middleware = None
    auth._templates = None


@pytest.fixture
def app(admin_user):
    """Create a FastAPI test app with the auth router and admin middleware."""
    test_app = FastAPI()

    @test_app.middleware("http")
    async def inject_admin_user(request: Request, call_next):
        """Inject admin user into request.state for testing protected routes."""
        request.state.user = admin_user
        request.state.session_token = "test-token"
        return await call_next(request)

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
# GET /api/admin/users
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_users_empty(client):
    """GET /api/admin/users returns empty list when no users exist."""
    response = await client.get("/api/admin/users")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.anyio
async def test_list_users_returns_created_users(client, auth_service):
    """GET /api/admin/users returns all created users."""
    await auth_service.create_user(
        email="user1@test.com",
        display_name="User One",
        password="password123",
        role="data_entry",
    )
    await auth_service.create_user(
        email="user2@test.com",
        display_name="User Two",
        password="password456",
        role="admin",
    )

    response = await client.get("/api/admin/users")
    assert response.status_code == 200
    users = response.json()
    assert len(users) == 2
    assert users[0]["email"] == "user1@test.com"
    assert users[0]["role"] == "data_entry"
    assert users[0]["active"] is True
    assert users[1]["email"] == "user2@test.com"
    assert users[1]["role"] == "admin"
    # Verify no password-related fields are exposed
    assert "password" not in users[0]
    assert "password_hash" not in users[0]


# ---------------------------------------------------------------------------
# POST /api/admin/users
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_user_success(client):
    """POST /api/admin/users creates a user and returns 201."""
    response = await client.post(
        "/api/admin/users",
        json={
            "email": "newuser@test.com",
            "display_name": "New User",
            "password": "securepass1",
            "role": "data_entry",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["email"] == "newuser@test.com"
    assert data["display_name"] == "New User"
    assert data["role"] == "data_entry"
    assert data["active"] is True
    assert "id" in data
    assert "created_at" in data
    # No password in response
    assert "password" not in data
    assert "password_hash" not in data


@pytest.mark.anyio
async def test_create_user_normalizes_email(client):
    """POST /api/admin/users normalizes email to lowercase."""
    response = await client.post(
        "/api/admin/users",
        json={
            "email": "UPPER@CASE.COM",
            "display_name": "Test",
            "password": "password123",
            "role": "admin",
        },
    )
    assert response.status_code == 201
    assert response.json()["email"] == "upper@case.com"


@pytest.mark.anyio
async def test_create_user_duplicate_email_409(client):
    """POST /api/admin/users returns 409 for duplicate email."""
    user_data = {
        "email": "dupe@test.com",
        "display_name": "First",
        "password": "password123",
        "role": "data_entry",
    }
    response1 = await client.post("/api/admin/users", json=user_data)
    assert response1.status_code == 201

    user_data["display_name"] = "Second"
    response2 = await client.post("/api/admin/users", json=user_data)
    assert response2.status_code == 409
    assert "already exists" in response2.json()["detail"]


@pytest.mark.anyio
async def test_create_user_validation_error_short_password(client):
    """POST /api/admin/users returns 422 for password too short."""
    response = await client.post(
        "/api/admin/users",
        json={
            "email": "valid@email.com",
            "display_name": "Test",
            "password": "short",
            "role": "data_entry",
        },
    )
    assert response.status_code == 422


@pytest.mark.anyio
async def test_create_user_validation_error_invalid_role(client):
    """POST /api/admin/users returns 422 for invalid role."""
    response = await client.post(
        "/api/admin/users",
        json={
            "email": "valid@email.com",
            "display_name": "Test",
            "password": "password123",
            "role": "superadmin",
        },
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# PUT /api/admin/users/{user_id}
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_update_user_display_name(client, auth_service):
    """PUT /api/admin/users/{id} updates display_name."""
    user = await auth_service.create_user(
        email="update@test.com",
        display_name="Old Name",
        password="password123",
        role="data_entry",
    )

    response = await client.put(
        f"/api/admin/users/{user.id}",
        json={"display_name": "New Name"},
    )
    assert response.status_code == 200
    assert response.json()["display_name"] == "New Name"
    assert response.json()["role"] == "data_entry"  # unchanged


@pytest.mark.anyio
async def test_update_user_role(client, auth_service):
    """PUT /api/admin/users/{id} updates role."""
    user = await auth_service.create_user(
        email="role@test.com",
        display_name="Role Test",
        password="password123",
        role="data_entry",
    )

    response = await client.put(
        f"/api/admin/users/{user.id}",
        json={"role": "admin"},
    )
    assert response.status_code == 200
    assert response.json()["role"] == "admin"


@pytest.mark.anyio
async def test_update_user_deactivate(client, auth_service):
    """PUT /api/admin/users/{id} with active=false deactivates user."""
    # Create a "padding" user so the target user gets id > 1
    # (admin_user mock has id=1)
    await auth_service.create_user(
        email="padding@test.com",
        display_name="Padding",
        password="password123",
        role="data_entry",
    )
    user = await auth_service.create_user(
        email="deactivate@test.com",
        display_name="Deactivate Me",
        password="password123",
        role="data_entry",
    )

    response = await client.put(
        f"/api/admin/users/{user.id}",
        json={"active": False},
    )
    assert response.status_code == 200
    assert response.json()["active"] is False


@pytest.mark.anyio
async def test_update_user_not_found_404(client):
    """PUT /api/admin/users/{id} returns 404 for non-existent user."""
    response = await client.put(
        "/api/admin/users/99999",
        json={"display_name": "Nobody"},
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_update_user_self_demotion_rejected(client, admin_user, auth_service):
    """PUT /api/admin/users/{id} rejects changing own role."""
    # admin_user.id == 1, so trying to change role of user 1
    # First create a user with id that matches admin_user.id
    user = await auth_service.create_user(
        email="admin@example.com",
        display_name="Admin",
        password="password123",
        role="admin",
    )
    # The admin_user mock has id=1, the created user gets id=1
    admin_user.id = user.id

    response = await client.put(
        f"/api/admin/users/{user.id}",
        json={"role": "data_entry"},
    )
    assert response.status_code == 400
    assert "self-demotion" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_update_user_self_deactivation_rejected(client, admin_user, auth_service):
    """PUT /api/admin/users/{id} rejects deactivating own account."""
    user = await auth_service.create_user(
        email="admin@example.com",
        display_name="Admin",
        password="password123",
        role="admin",
    )
    admin_user.id = user.id

    response = await client.put(
        f"/api/admin/users/{user.id}",
        json={"active": False},
    )
    assert response.status_code == 400
    assert "self-deactivation" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_update_user_deactivation_invalidates_sessions(client, auth_service):
    """Deactivating a user invalidates all their sessions."""
    # Create padding user so target doesn't get id=1 (matching admin_user)
    await auth_service.create_user(
        email="padding@test.com",
        display_name="Padding",
        password="password123",
        role="data_entry",
    )
    user = await auth_service.create_user(
        email="sessions@test.com",
        display_name="Session Test",
        password="password123",
        role="data_entry",
    )

    # Create some sessions for this user
    token1 = await auth_service.create_session(user.id, client_ip="1.2.3.4")
    token2 = await auth_service.create_session(user.id, client_ip="1.2.3.4")

    # Verify sessions are valid
    validated1 = await auth_service.validate_session(token1, client_ip="1.2.3.4")
    assert validated1 is not None

    # Deactivate the user
    response = await client.put(
        f"/api/admin/users/{user.id}",
        json={"active": False},
    )
    assert response.status_code == 200

    # Verify sessions are now invalidated
    validated1_after = await auth_service.validate_session(token1, client_ip="1.2.3.4")
    validated2_after = await auth_service.validate_session(token2, client_ip="1.2.3.4")
    assert validated1_after is None
    assert validated2_after is None


@pytest.mark.anyio
async def test_update_user_password(client, auth_service):
    """PUT /api/admin/users/{id} with password updates the hash."""
    user = await auth_service.create_user(
        email="pwchange@test.com",
        display_name="PW Change",
        password="oldpassword1",
        role="data_entry",
    )

    response = await client.put(
        f"/api/admin/users/{user.id}",
        json={"password": "newpassword1"},
    )
    assert response.status_code == 200

    # Verify new password works for authentication
    authenticated = await auth_service.authenticate("pwchange@test.com", "newpassword1")
    assert authenticated is not None
    assert authenticated.id == user.id

    # Verify old password no longer works
    old_auth = await auth_service.authenticate("pwchange@test.com", "oldpassword1")
    assert old_auth is None


# ---------------------------------------------------------------------------
# GET /admin/users (HTML page)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_users_page_renders(client, mock_templates, auth_service):
    """GET /admin/users renders the user management page."""
    await auth_service.create_user(
        email="page@test.com",
        display_name="Page User",
        password="password123",
        role="data_entry",
    )

    response = await client.get("/admin/users")
    assert response.status_code == 200
    mock_templates.TemplateResponse.assert_called_once()


# ---------------------------------------------------------------------------
# Audit logging for user management
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_user_audits(client):
    """POST /api/admin/users emits an audit log entry."""
    with patch("flight_card_scanner.routers.auth.log_action") as mock_log:
        response = await client.post(
            "/api/admin/users",
            json={
                "email": "audited@test.com",
                "display_name": "Audited",
                "password": "password123",
                "role": "data_entry",
            },
        )
        assert response.status_code == 201
        created_calls = [
            c for c in mock_log.call_args_list
            if "action" in c[1] and c[1]["action"] == "created"
        ]
        assert len(created_calls) == 1
        assert created_calls[0][1]["object_type"] == "user"
        assert created_calls[0][1]["actor"] == "admin@example.com"


@pytest.mark.anyio
async def test_update_user_audits(client, auth_service):
    """PUT /api/admin/users/{id} emits an audit log entry with changes."""
    user = await auth_service.create_user(
        email="auditupdate@test.com",
        display_name="Before",
        password="password123",
        role="data_entry",
    )

    with patch("flight_card_scanner.routers.auth.log_action") as mock_log:
        response = await client.put(
            f"/api/admin/users/{user.id}",
            json={"display_name": "After"},
        )
        assert response.status_code == 200
        updated_calls = [
            c for c in mock_log.call_args_list
            if "action" in c[1] and c[1]["action"] == "updated"
        ]
        assert len(updated_calls) == 1
        details = updated_calls[0][1]["details"]
        assert "changes" in details
        assert details["changes"]["display_name"]["old"] == "Before"
        assert details["changes"]["display_name"]["new"] == "After"


# ---------------------------------------------------------------------------
# Authorization enforcement (unauthenticated)
# ---------------------------------------------------------------------------


@pytest.fixture
def unauthenticated_app():
    """Create a FastAPI app without admin middleware (unauthenticated)."""
    test_app = FastAPI()

    @test_app.middleware("http")
    async def inject_no_user(request: Request, call_next):
        """Inject no user into request.state."""
        request.state.user = None
        request.state.session_token = None
        return await call_next(request)

    test_app.include_router(router)
    return test_app


@pytest.fixture
async def unauthenticated_client(unauthenticated_app):
    """Client without authentication."""
    transport = ASGITransport(app=unauthenticated_app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        follow_redirects=False,
        headers={"Accept": "application/json"},
    ) as ac:
        yield ac


@pytest.mark.anyio
async def test_list_users_unauthenticated_401(unauthenticated_client):
    """GET /api/admin/users without auth returns 401."""
    response = await unauthenticated_client.get("/api/admin/users")
    assert response.status_code == 401


@pytest.mark.anyio
async def test_create_user_unauthenticated_401(unauthenticated_client):
    """POST /api/admin/users without auth returns 401."""
    response = await unauthenticated_client.post(
        "/api/admin/users",
        json={
            "email": "test@test.com",
            "display_name": "Test",
            "password": "password123",
            "role": "data_entry",
        },
    )
    assert response.status_code == 401
