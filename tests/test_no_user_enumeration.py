"""Unit tests for no user enumeration (Property 9: No User Enumeration).

Verifies that:
- authenticate() returns identical results (None) for non-existent vs existing
  emails with wrong passwords
- Timing difference between the two cases is within 100ms (timing attack prevention)
- HTTP login endpoint returns the same error message and status code regardless
  of whether the email exists

# Feature: auth-and-audit, Property 9: No User Enumeration
# Validates: Requirements 2.3, 8.5
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from flight_card_scanner.auth_models import AuthBase
from flight_card_scanner.routers import auth
from flight_card_scanner.routers.auth import router
from flight_card_scanner.services.auth_service import AuthService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    """Create a mock Jinja2Templates that returns a simple HTML response."""
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
def configure_auth_router(auth_service, mock_session_middleware, mock_templates):
    """Wire up real auth service for enumeration tests."""
    auth.configure(
        auth_service=auth_service,
        session_middleware=mock_session_middleware,
        templates=mock_templates,
    )
    yield
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
# Service-level tests: authenticate() return values
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_authenticate_returns_none_for_nonexistent_email(auth_service):
    """authenticate() returns None for a non-existent email."""
    result = await auth_service.authenticate("nonexistent@fake.com", "wrongpassword")
    assert result is None


@pytest.mark.anyio
async def test_authenticate_returns_none_for_wrong_password(auth_service):
    """authenticate() returns None for an existing email with wrong password."""
    await auth_service.create_user(
        email="real@example.com",
        display_name="Real User",
        password="correctpassword",
        role="data_entry",
    )
    result = await auth_service.authenticate("real@example.com", "wrongpassword")
    assert result is None


@pytest.mark.anyio
async def test_identical_response_for_existing_vs_nonexistent(auth_service):
    """authenticate() returns identical result (None) regardless of email existence."""
    await auth_service.create_user(
        email="real@example.com",
        display_name="Real User",
        password="password123",
        role="data_entry",
    )

    result_existing = await auth_service.authenticate("real@example.com", "wrongpassword")
    result_nonexistent = await auth_service.authenticate("nonexistent@fake.com", "wrongpassword")

    # Both must return None — identical response
    assert result_existing is None
    assert result_nonexistent is None
    assert result_existing == result_nonexistent


# ---------------------------------------------------------------------------
# Service-level tests: timing attack prevention
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_timing_difference_within_100ms(auth_service):
    """Timing difference between existing vs non-existing email is < 100ms.

    The auth service always runs argon2 verify even for non-existent emails,
    so the response time should be nearly identical.
    """
    await auth_service.create_user(
        email="real@example.com",
        display_name="Real User",
        password="password123",
        role="data_entry",
    )

    # Warm up the dummy hash (first call initializes it)
    await auth_service.authenticate("warmup@nonexistent.com", "warmup")

    # Measure time for existing email with wrong password
    iterations = 5
    existing_times = []
    for _ in range(iterations):
        start = time.perf_counter()
        await auth_service.authenticate("real@example.com", "wrongpassword")
        existing_times.append(time.perf_counter() - start)

    # Measure time for non-existing email
    nonexistent_times = []
    for _ in range(iterations):
        start = time.perf_counter()
        await auth_service.authenticate("nonexistent@fake.com", "wrongpassword")
        nonexistent_times.append(time.perf_counter() - start)

    avg_existing = sum(existing_times) / len(existing_times)
    avg_nonexistent = sum(nonexistent_times) / len(nonexistent_times)
    timing_diff_ms = abs(avg_existing - avg_nonexistent) * 1000

    # Timing difference must be within 100ms
    assert timing_diff_ms < 100, (
        f"Timing difference {timing_diff_ms:.2f}ms exceeds 100ms threshold. "
        f"avg_existing={avg_existing*1000:.2f}ms, "
        f"avg_nonexistent={avg_nonexistent*1000:.2f}ms"
    )


# ---------------------------------------------------------------------------
# HTTP-level tests: identical response body and status code
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_login_same_status_code_existing_vs_nonexistent(client, auth_service):
    """POST /login returns same HTTP status for existing vs non-existing email."""
    await auth_service.create_user(
        email="real@example.com",
        display_name="Real User",
        password="password123",
        role="data_entry",
    )

    response_existing = await client.post(
        "/login",
        data={"email": "real@example.com", "password": "wrongpassword"},
    )
    response_nonexistent = await client.post(
        "/login",
        data={"email": "nonexistent@fake.com", "password": "wrongpassword"},
    )

    # Same status code
    assert response_existing.status_code == response_nonexistent.status_code


@pytest.mark.anyio
async def test_login_same_error_message_existing_vs_nonexistent(
    client, auth_service, mock_templates
):
    """POST /login returns same error message regardless of email existence."""
    await auth_service.create_user(
        email="real@example.com",
        display_name="Real User",
        password="password123",
        role="data_entry",
    )

    # First request with existing email + wrong password
    await client.post(
        "/login",
        data={"email": "real@example.com", "password": "wrongpassword"},
    )
    call_existing = mock_templates.TemplateResponse.call_args
    context_existing = call_existing.kwargs.get("context", {})
    error_existing = context_existing.get("error", "")

    # Reset mock
    mock_templates.TemplateResponse.reset_mock()

    # Second request with non-existing email
    await client.post(
        "/login",
        data={"email": "nonexistent@fake.com", "password": "wrongpassword"},
    )
    call_nonexistent = mock_templates.TemplateResponse.call_args
    context_nonexistent = call_nonexistent.kwargs.get("context", {})
    error_nonexistent = context_nonexistent.get("error", "")

    # Error messages must be identical (no user enumeration)
    assert error_existing == error_nonexistent
    # Both should contain the generic error
    assert "Invalid email or password" in error_existing
    assert "Invalid email or password" in error_nonexistent


@pytest.mark.anyio
async def test_login_same_response_body_existing_vs_nonexistent(client, auth_service):
    """POST /login returns identical response body structure regardless of email existence."""
    await auth_service.create_user(
        email="real@example.com",
        display_name="Real User",
        password="password123",
        role="data_entry",
    )

    response_existing = await client.post(
        "/login",
        data={"email": "real@example.com", "password": "wrongpassword"},
    )
    response_nonexistent = await client.post(
        "/login",
        data={"email": "nonexistent@fake.com", "password": "wrongpassword"},
    )

    # Response bodies should be identical (same error rendered)
    assert response_existing.text == response_nonexistent.text
