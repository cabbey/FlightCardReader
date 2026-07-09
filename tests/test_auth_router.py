"""Unit tests for the auth router (login/logout endpoints)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from httpx import ASGITransport, AsyncClient

from flight_card_scanner.routers import auth
from flight_card_scanner.routers.auth import router


@pytest.fixture
def mock_user():
    """Create a mock authenticated user."""
    user = MagicMock()
    user.id = 1
    user.email = "test@example.com"
    user.display_name = "Test User"
    user.role = "data_entry"
    user.active = True
    return user


@pytest.fixture
def mock_auth_service():
    """Create a mock AuthService."""
    svc = AsyncMock()
    svc.check_rate_limit = MagicMock(return_value=(False, 0))
    svc.authenticate = AsyncMock(return_value=None)
    svc.create_session = AsyncMock(return_value="test-session-token-abc")
    svc.invalidate_session = AsyncMock()
    svc.record_failed_attempt = MagicMock()
    svc.reset_failed_attempts = MagicMock()
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


@pytest.fixture
def mock_templates():
    """Create a mock Jinja2Templates that returns a simple HTML response."""
    templates = MagicMock()

    def fake_template_response(*args, **kwargs):
        """Accept positional or keyword args matching Jinja2Templates.TemplateResponse."""
        # Handle both positional and keyword-only call patterns
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


@pytest.fixture(autouse=True)
def configure_auth(mock_auth_service, mock_session_middleware, mock_templates):
    """Wire up mock dependencies for the auth router."""
    auth.configure(
        auth_service=mock_auth_service,
        session_middleware=mock_session_middleware,
        templates=mock_templates,
    )
    yield
    # Reset module state
    auth._auth_service = None
    auth._session_middleware = None
    auth._templates = None


@pytest.fixture
def app():
    """Create a FastAPI test app with the auth router included."""
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
# GET /login
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_login_page_renders(client, mock_templates):
    """GET /login renders the login form template."""
    response = await client.get("/login")
    assert response.status_code == 200
    mock_templates.TemplateResponse.assert_called_once()
    call_kwargs = mock_templates.TemplateResponse.call_args.kwargs
    assert call_kwargs["name"] == "login.html"
    context = call_kwargs["context"]
    # Error should be empty/falsy on initial render
    assert not context["error"]


@pytest.mark.anyio
async def test_login_page_passes_next_param(client, mock_templates):
    """GET /login?next=/admin passes the next param to the template context."""
    response = await client.get("/login?next=/admin")
    assert response.status_code == 200
    call_kwargs = mock_templates.TemplateResponse.call_args.kwargs
    context = call_kwargs["context"]
    assert context["next"] == "/admin"


# ---------------------------------------------------------------------------
# POST /login — successful authentication
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_login_success_redirects(
    client, mock_auth_service, mock_session_middleware, mock_user
):
    """POST /login with valid credentials redirects to / and sets cookie."""
    mock_auth_service.authenticate = AsyncMock(return_value=mock_user)

    response = await client.post(
        "/login",
        data={"email": "test@example.com", "password": "correctpassword"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"

    # Verify cookie is set
    assert "set-cookie" in response.headers
    assert "fcs_session" in response.headers["set-cookie"]

    # Verify session was created
    mock_auth_service.create_session.assert_awaited_once()
    mock_session_middleware.sign_token.assert_called_once_with("test-session-token-abc")

    # Verify failed attempts were reset
    mock_auth_service.reset_failed_attempts.assert_called_once_with("test@example.com")


@pytest.mark.anyio
async def test_login_success_redirects_to_next(
    client, mock_auth_service, mock_user
):
    """POST /login with next param redirects to that URL on success."""
    mock_auth_service.authenticate = AsyncMock(return_value=mock_user)

    response = await client.post(
        "/login",
        data={
            "email": "test@example.com",
            "password": "correctpassword",
            "next": "/scan",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/scan"


# ---------------------------------------------------------------------------
# POST /login — failed authentication
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_login_failure_shows_generic_error(
    client, mock_auth_service, mock_templates
):
    """POST /login with invalid credentials returns 401 with generic error."""
    mock_auth_service.authenticate = AsyncMock(return_value=None)

    response = await client.post(
        "/login",
        data={"email": "bad@example.com", "password": "wrongpass"},
    )
    assert response.status_code == 401

    call_kwargs = mock_templates.TemplateResponse.call_args.kwargs
    context = call_kwargs["context"]
    assert "Invalid email or password" in context["error"]
    # Records the failed attempt
    mock_auth_service.record_failed_attempt.assert_called_once_with("bad@example.com")


@pytest.mark.anyio
async def test_login_failure_no_user_enumeration(
    client, mock_auth_service, mock_templates
):
    """POST /login with non-existent email returns same error as wrong password."""
    mock_auth_service.authenticate = AsyncMock(return_value=None)

    response = await client.post(
        "/login",
        data={"email": "nonexistent@nowhere.com", "password": "anything"},
    )
    assert response.status_code == 401
    call_kwargs = mock_templates.TemplateResponse.call_args.kwargs
    context = call_kwargs["context"]
    # Same generic message regardless of whether email exists
    assert "Invalid email or password" in context["error"]


# ---------------------------------------------------------------------------
# POST /login — rate limiting
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_login_rate_limited(client, mock_auth_service, mock_templates):
    """POST /login when rate limited returns 429."""
    mock_auth_service.check_rate_limit = MagicMock(return_value=(True, 600))

    response = await client.post(
        "/login",
        data={"email": "test@example.com", "password": "password"},
    )
    assert response.status_code == 429

    # Should NOT attempt authentication when rate limited
    mock_auth_service.authenticate.assert_not_awaited()


# ---------------------------------------------------------------------------
# GET /logout
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_logout_redirects_to_login(client, mock_session_middleware):
    """GET /logout redirects to /login and clears cookie."""
    response = await client.get("/logout")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"

    # Verify clear cookie header is set
    assert "set-cookie" in response.headers
    assert "Max-Age=0" in response.headers["set-cookie"]


@pytest.mark.anyio
async def test_logout_invalidates_session(client, mock_auth_service):
    """GET /logout works gracefully when no session exists."""
    # Without the session middleware running, request.state won't have session_token.
    # This tests that logout handles that case gracefully.
    response = await client.get("/logout")
    assert response.status_code == 303


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_login_success_audits(client, mock_auth_service, mock_user):
    """Successful login emits an audit log entry."""
    mock_auth_service.authenticate = AsyncMock(return_value=mock_user)

    with patch("flight_card_scanner.routers.auth.log_action") as mock_log:
        response = await client.post(
            "/login",
            data={"email": "test@example.com", "password": "password"},
        )
        assert response.status_code == 303
        # Find the login success audit call
        mock_log.assert_called()
        calls = [
            c for c in mock_log.call_args_list
            if c[1].get("action") == "login" or (c[0] and len(c[0]) > 1 and c[0][1] == "login")
        ]
        # Check via keyword args
        login_calls = [
            c for c in mock_log.call_args_list
            if "action" in c[1] and c[1]["action"] == "login"
        ]
        assert len(login_calls) == 1
        assert login_calls[0][1]["actor"] == "test@example.com"
        assert login_calls[0][1]["details"]["result"] == "success"


@pytest.mark.anyio
async def test_login_failure_audits(client, mock_auth_service):
    """Failed login emits a login_failed audit log entry."""
    mock_auth_service.authenticate = AsyncMock(return_value=None)

    with patch("flight_card_scanner.routers.auth.log_action") as mock_log:
        response = await client.post(
            "/login",
            data={"email": "hacker@evil.com", "password": "wrong"},
        )
        assert response.status_code == 401
        login_failed_calls = [
            c for c in mock_log.call_args_list
            if "action" in c[1] and c[1]["action"] == "login_failed"
        ]
        assert len(login_failed_calls) == 1
        assert login_failed_calls[0][1]["actor"] == "hacker@evil.com"
        assert login_failed_calls[0][1]["details"]["result"] == "failed"
