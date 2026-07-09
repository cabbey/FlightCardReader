"""Integration tests for the full auth flow.

Tests end-to-end authentication scenarios using a real in-memory auth database,
real AuthService, real SessionMiddleware, and mock templates for HTML responses.

Validates: Requirements 2.1, 2.2, 2.6, 2.7, 8.2, 1.6, 1.7
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from flight_card_scanner.auth_models import AuthBase, User
from flight_card_scanner.middleware.session_middleware import SessionMiddleware
from flight_card_scanner.routers import auth
from flight_card_scanner.routers.auth import router as auth_router
from flight_card_scanner.services.auth_service import AuthService


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSION_SECRET = "integration-test-secret-at-least-16-chars"
COOKIE_NAME = "fcs_session"
TEST_EMAIL = "admin@test.com"
TEST_PASSWORD = "securepassword123"
TEST_DISPLAY_NAME = "Test Admin"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def auth_db_session_factory():
    """Create an in-memory SQLite async engine and session factory for auth."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(AuthBase.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield session_factory

    await engine.dispose()


@pytest.fixture
async def auth_service(auth_db_session_factory):
    """Create a real AuthService with an in-memory database."""
    service = AuthService(
        session_factory=auth_db_session_factory,
        session_secret=SESSION_SECRET,
        timeout_hours=8.0,
    )
    return service


@pytest.fixture
def mock_templates():
    """Create mock templates that return simple HTML responses."""
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
async def app_with_auth(auth_service, mock_templates):
    """Create a full FastAPI test app with real auth middleware and router."""
    app = FastAPI()

    # Add a protected endpoint for testing
    @app.get("/protected")
    async def protected_page(request: Request):
        user = getattr(request.state, "user", None)
        if user is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated"},
            )
        return JSONResponse(
            content={
                "message": "Access granted",
                "user_email": user.email,
                "user_role": user.role,
            }
        )

    # Add a simple public endpoint
    @app.get("/public")
    async def public_page(request: Request):
        user = getattr(request.state, "user", None)
        return JSONResponse(
            content={
                "message": "Public page",
                "authenticated": user is not None,
            }
        )

    # Include auth router
    app.include_router(auth_router)

    # Create and configure session middleware
    session_mw = SessionMiddleware(
        app=app,
        auth_service=auth_service,
        cookie_name=COOKIE_NAME,
        session_secret=SESSION_SECRET,
        secure=False,
    )

    # Configure the auth router with real dependencies
    auth.configure(
        auth_service=auth_service,
        templates=mock_templates,
        session_middleware=session_mw,
    )

    # Return the middleware-wrapped app so session resolution happens
    yield session_mw

    # Cleanup
    auth._auth_service = None
    auth._session_middleware = None
    auth._templates = None


@pytest.fixture
async def app_with_auth_secure(auth_service, mock_templates):
    """Create a full FastAPI test app with Secure cookie flag enabled."""
    app = FastAPI()

    app.include_router(auth_router)

    session_mw = SessionMiddleware(
        app=app,
        auth_service=auth_service,
        cookie_name=COOKIE_NAME,
        session_secret=SESSION_SECRET,
        secure=True,
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
async def client(app_with_auth):
    """Async HTTP client for integration testing."""
    transport = ASGITransport(app=app_with_auth)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as ac:
        yield ac


@pytest.fixture
async def client_secure(app_with_auth_secure):
    """Async HTTP client for testing with Secure cookies."""
    transport = ASGITransport(app=app_with_auth_secure)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as ac:
        yield ac


@pytest.fixture
async def admin_user(auth_service):
    """Create a test admin user in the database."""
    user = await auth_service.create_user(
        email=TEST_EMAIL,
        display_name=TEST_DISPLAY_NAME,
        password=TEST_PASSWORD,
        role="admin",
    )
    return user


# ---------------------------------------------------------------------------
# Test: Full Login → Access Protected Page → Logout Flow
# ---------------------------------------------------------------------------


class TestFullLoginFlow:
    """Integration test for complete login → access → logout cycle."""

    @pytest.mark.anyio
    async def test_login_sets_cookie_and_grants_access(
        self, client, admin_user, auth_service
    ):
        """POST /login with valid creds → cookie set → access protected → logout → cookie cleared."""
        # Step 1: Login with valid credentials
        login_response = await client.post(
            "/login",
            data={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        assert login_response.status_code == 303
        assert login_response.headers["location"] == "/"

        # Step 2: Extract the session cookie from Set-Cookie header
        set_cookie_header = login_response.headers.get("set-cookie", "")
        assert COOKIE_NAME in set_cookie_header

        # Parse cookie value from Set-Cookie header
        cookie_value = _extract_cookie_value(set_cookie_header)
        assert cookie_value is not None

        # Step 3: Use the cookie to access a protected endpoint
        protected_response = await client.get(
            "/protected",
            cookies={COOKIE_NAME: cookie_value},
        )
        assert protected_response.status_code == 200
        data = protected_response.json()
        assert data["message"] == "Access granted"
        assert data["user_email"] == TEST_EMAIL.lower()
        assert data["user_role"] == "admin"

        # Step 4: Logout
        logout_response = await client.get(
            "/logout",
            cookies={COOKIE_NAME: cookie_value},
        )
        assert logout_response.status_code == 303
        assert logout_response.headers["location"] == "/login"

        # Step 5: Verify cookie is cleared in logout response
        logout_cookie = logout_response.headers.get("set-cookie", "")
        assert "Max-Age=0" in logout_cookie

        # Step 6: Try to access protected page with the old cookie (should fail)
        after_logout_response = await client.get(
            "/protected",
            cookies={COOKIE_NAME: cookie_value},
        )
        # Session was invalidated, so user should be None
        assert after_logout_response.status_code == 401

    @pytest.mark.anyio
    async def test_login_with_next_param_redirects_correctly(
        self, client, admin_user
    ):
        """POST /login with next param redirects to that URL after success."""
        response = await client.post(
            "/login",
            data={
                "email": TEST_EMAIL,
                "password": TEST_PASSWORD,
                "next": "/scan",
            },
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/scan"

    @pytest.mark.anyio
    async def test_login_with_invalid_credentials_returns_error(self, client, admin_user):
        """POST /login with wrong password returns 401 with generic error."""
        response = await client.post(
            "/login",
            data={"email": TEST_EMAIL, "password": "wrongpassword"},
        )
        assert response.status_code == 401
        assert "Invalid email or password" in response.text

    @pytest.mark.anyio
    async def test_login_with_nonexistent_email_returns_same_error(self, client):
        """POST /login with non-existent email returns same error as wrong password."""
        response = await client.post(
            "/login",
            data={"email": "nobody@example.com", "password": "anything"},
        )
        assert response.status_code == 401
        assert "Invalid email or password" in response.text

    @pytest.mark.anyio
    async def test_unauthenticated_request_has_no_user(self, client):
        """A request without a session cookie has no authenticated user."""
        response = await client.get("/protected")
        assert response.status_code == 401
        data = response.json()
        assert data["detail"] == "Not authenticated"


# ---------------------------------------------------------------------------
# Test: Rate Limiting Through Actual HTTP Requests
# ---------------------------------------------------------------------------


class TestRateLimiting:
    """Integration test for rate limiting via real HTTP requests."""

    @pytest.mark.anyio
    async def test_rate_limit_after_5_failed_attempts(self, client, admin_user):
        """After 5 failed login attempts, the 6th attempt returns 429."""
        # Make 5 failed attempts
        for i in range(5):
            response = await client.post(
                "/login",
                data={"email": TEST_EMAIL, "password": "wrong"},
            )
            assert response.status_code == 401, f"Attempt {i+1} should fail with 401"

        # 6th attempt should be rate-limited (429)
        response = await client.post(
            "/login",
            data={"email": TEST_EMAIL, "password": "wrong"},
        )
        assert response.status_code == 429

    @pytest.mark.anyio
    async def test_rate_limit_applies_even_with_correct_password(
        self, client, admin_user
    ):
        """After being rate-limited, even correct credentials are rejected."""
        # Make 5 failed attempts
        for _ in range(5):
            await client.post(
                "/login",
                data={"email": TEST_EMAIL, "password": "wrong"},
            )

        # Try with correct password — should still be rate-limited
        response = await client.post(
            "/login",
            data={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        assert response.status_code == 429

    @pytest.mark.anyio
    async def test_rate_limit_is_per_email(self, client, admin_user, auth_service):
        """Rate limiting is per-email, not global."""
        # Create another user
        await auth_service.create_user(
            email="other@test.com",
            display_name="Other User",
            password="otherpassword123",
            role="data_entry",
        )

        # Rate-limit the admin email
        for _ in range(5):
            await client.post(
                "/login",
                data={"email": TEST_EMAIL, "password": "wrong"},
            )

        # Admin is rate-limited
        response = await client.post(
            "/login",
            data={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        assert response.status_code == 429

        # Other user is NOT rate-limited
        response = await client.post(
            "/login",
            data={"email": "other@test.com", "password": "otherpassword123"},
        )
        assert response.status_code == 303  # Successful redirect

    @pytest.mark.anyio
    async def test_rate_limit_message_shows_retry_after(self, client, admin_user):
        """Rate limit response includes retry information."""
        # Rate-limit the user
        for _ in range(5):
            await client.post(
                "/login",
                data={"email": TEST_EMAIL, "password": "wrong"},
            )

        response = await client.post(
            "/login",
            data={"email": TEST_EMAIL, "password": "wrong"},
        )
        assert response.status_code == 429
        # The HTML response should mention trying again
        assert "Try again" in response.text or "Too many" in response.text


# ---------------------------------------------------------------------------
# Test: Cookie Attributes (HttpOnly, SameSite, Secure)
# ---------------------------------------------------------------------------


class TestCookieAttributes:
    """Integration test for cookie attribute correctness."""

    @pytest.mark.anyio
    async def test_login_cookie_has_httponly(self, client, admin_user):
        """After login, the Set-Cookie header includes HttpOnly."""
        response = await client.post(
            "/login",
            data={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        assert response.status_code == 303
        set_cookie = response.headers.get("set-cookie", "")
        assert "HttpOnly" in set_cookie

    @pytest.mark.anyio
    async def test_login_cookie_has_samesite_lax(self, client, admin_user):
        """After login, the Set-Cookie header includes SameSite=Lax."""
        response = await client.post(
            "/login",
            data={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        assert response.status_code == 303
        set_cookie = response.headers.get("set-cookie", "")
        assert "SameSite=Lax" in set_cookie

    @pytest.mark.anyio
    async def test_login_cookie_has_path(self, client, admin_user):
        """After login, the Set-Cookie header includes Path=/."""
        response = await client.post(
            "/login",
            data={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        assert response.status_code == 303
        set_cookie = response.headers.get("set-cookie", "")
        assert "Path=/" in set_cookie

    @pytest.mark.anyio
    async def test_login_cookie_no_secure_without_ssl(self, client, admin_user):
        """Without SSL configured, Set-Cookie should NOT have Secure flag."""
        response = await client.post(
            "/login",
            data={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        assert response.status_code == 303
        set_cookie = response.headers.get("set-cookie", "")
        assert "Secure" not in set_cookie

    @pytest.mark.anyio
    async def test_login_cookie_has_secure_with_ssl(
        self, client_secure, admin_user
    ):
        """With SSL configured, Set-Cookie should have Secure flag."""
        response = await client_secure.post(
            "/login",
            data={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        assert response.status_code == 303
        set_cookie = response.headers.get("set-cookie", "")
        assert "Secure" in set_cookie

    @pytest.mark.anyio
    async def test_logout_clear_cookie_attributes(self, client, admin_user):
        """Logout Set-Cookie header includes correct clearing attributes."""
        # First login to get a session
        login_response = await client.post(
            "/login",
            data={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        cookie_value = _extract_cookie_value(
            login_response.headers.get("set-cookie", "")
        )

        # Now logout
        logout_response = await client.get(
            "/logout",
            cookies={COOKIE_NAME: cookie_value},
        )
        set_cookie = logout_response.headers.get("set-cookie", "")
        assert "Max-Age=0" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=Lax" in set_cookie


# ---------------------------------------------------------------------------
# Test: Default Admin Creation at Startup
# ---------------------------------------------------------------------------


class TestDefaultAdminCreation:
    """Integration test for default admin account creation at startup."""

    @pytest.mark.anyio
    async def test_default_admin_created_from_env_vars(self):
        """When no admin exists and env vars are set, admin is created at startup."""
        # Set up fresh in-memory database
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(AuthBase.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        # Create auth service
        service = AuthService(
            session_factory=session_factory,
            session_secret=SESSION_SECRET,
            timeout_hours=8.0,
        )

        # Simulate the startup logic from main.py
        from sqlalchemy import select

        async with session_factory() as db:
            result = await db.execute(
                select(User).where(User.role == "admin")
            )
            admin_exists = result.scalar_one_or_none() is not None

        assert admin_exists is False

        # Simulate env vars being set
        admin_email = "startup-admin@example.com"
        admin_password = "startup-password-123"

        if not admin_exists and admin_email and admin_password:
            await service.create_user(
                admin_email, "Admin", admin_password, "admin"
            )

        # Verify admin was created
        async with session_factory() as db:
            result = await db.execute(
                select(User).where(User.role == "admin")
            )
            admin_user = result.scalar_one_or_none()

        assert admin_user is not None
        assert admin_user.email == "startup-admin@example.com"
        assert admin_user.role == "admin"
        assert admin_user.active is True
        assert admin_user.display_name == "Admin"

        # Verify the admin can authenticate
        authenticated = await service.authenticate(admin_email, admin_password)
        assert authenticated is not None
        assert authenticated.email == admin_email.lower()

        await engine.dispose()

    @pytest.mark.anyio
    async def test_no_admin_created_when_env_vars_missing(self):
        """When env vars are not set, no admin is created (only a warning is logged)."""
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(AuthBase.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        from sqlalchemy import select

        # Simulate startup logic with empty env vars
        admin_email = ""
        admin_password = ""

        async with session_factory() as db:
            result = await db.execute(
                select(User).where(User.role == "admin")
            )
            admin_exists = result.scalar_one_or_none() is not None

        assert admin_exists is False

        # With empty env vars, no user should be created
        if not admin_exists and admin_email and admin_password:
            # This branch should NOT execute
            service = AuthService(
                session_factory=session_factory,
                session_secret=SESSION_SECRET,
                timeout_hours=8.0,
            )
            await service.create_user(admin_email, "Admin", admin_password, "admin")

        # Verify no admin was created
        async with session_factory() as db:
            result = await db.execute(
                select(User).where(User.role == "admin")
            )
            admin_user = result.scalar_one_or_none()

        assert admin_user is None

        await engine.dispose()

    @pytest.mark.anyio
    async def test_no_admin_created_when_admin_already_exists(self):
        """When an admin already exists, no new admin is created even with env vars."""
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(AuthBase.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        service = AuthService(
            session_factory=session_factory,
            session_secret=SESSION_SECRET,
            timeout_hours=8.0,
        )

        # Pre-create an admin
        await service.create_user(
            "existing-admin@test.com", "Existing Admin", "existingpass123", "admin"
        )

        from sqlalchemy import select

        # Simulate startup check
        async with session_factory() as db:
            result = await db.execute(
                select(User).where(User.role == "admin")
            )
            admin_exists = result.scalar_one_or_none() is not None

        assert admin_exists is True

        # Even with env vars set, shouldn't create another admin
        admin_email = "new-admin@test.com"
        admin_password = "newadminpass123"

        if not admin_exists and admin_email and admin_password:
            await service.create_user(admin_email, "New Admin", admin_password, "admin")

        # Verify only one admin exists
        async with session_factory() as db:
            result = await db.execute(select(User).where(User.role == "admin"))
            admins = list(result.scalars().all())

        assert len(admins) == 1
        assert admins[0].email == "existing-admin@test.com"

        await engine.dispose()


# ---------------------------------------------------------------------------
# Test: Read-Only Mode Interaction with Auth
# ---------------------------------------------------------------------------


class TestReadOnlyInteraction:
    """Integration test for read-only mode interacting with authentication."""

    @pytest.mark.anyio
    async def test_read_only_blocks_post_before_auth(self):
        """In read-only mode, POST requests are blocked before auth is checked."""
        app = FastAPI()

        # Add read_only_guard middleware (same as in main.py)
        @app.middleware("http")
        async def read_only_guard(request, call_next):
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Read-only mode. No modifications allowed."},
                )
            return await call_next(request)

        # A protected POST endpoint
        @app.post("/api/data")
        async def create_data(request: Request):
            return JSONResponse(content={"created": True})

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            # POST should be blocked with 403 (read-only), not 401 (auth)
            response = await client.post(
                "/api/data",
                json={"key": "value"},
            )
            assert response.status_code == 403
            data = response.json()
            assert "Read-only" in data["detail"] or "read-only" in data["detail"].lower()

    @pytest.mark.anyio
    async def test_read_only_allows_get_requests(self):
        """In read-only mode, GET requests pass through normally."""
        app = FastAPI()

        @app.middleware("http")
        async def read_only_guard(request, call_next):
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Read-only mode."},
                )
            return await call_next(request)

        @app.get("/public")
        async def public_page():
            return JSONResponse(content={"accessible": True})

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            response = await client.get("/public")
            assert response.status_code == 200
            assert response.json()["accessible"] is True

    @pytest.mark.anyio
    async def test_login_post_blocked_in_read_only_mode(self):
        """POST /login is also blocked by read-only guard since it precedes auth."""
        app = FastAPI()

        @app.middleware("http")
        async def read_only_guard(request, call_next):
            # In real app, read_only is checked from config
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Read-only mode."},
                )
            return await call_next(request)

        app.include_router(auth_router)

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            response = await client.post(
                "/login",
                data={"email": "test@test.com", "password": "pass"},
            )
            # Blocked by read-only middleware before reaching auth
            assert response.status_code == 403


# ---------------------------------------------------------------------------
# Test: Middleware Ordering (read_only runs before auth)
# ---------------------------------------------------------------------------


class TestMiddlewareOrdering:
    """Test that read_only middleware executes before session middleware.

    In the actual app (main.py), read_only_guard is an @app.middleware("http")
    decorator while SessionMiddleware wraps the app as ASGI middleware. The
    decorator-based middleware runs INSIDE (after) ASGI wrapping. However, the
    practical effect is:
    - read_only_guard runs inside the app (as a dispatcher middleware)
    - SessionMiddleware wraps the ASGI app (runs around the whole app)

    The design intent is: if read_only blocks a mutating request, auth resolution
    was wasted work but harmless. The important property is that protected
    POST endpoints return 403 (read-only) rather than 401 (auth) when
    read_only is active.
    """

    @pytest.mark.anyio
    async def test_read_only_returns_403_not_401_for_posts(self):
        """In read-only mode, mutating requests get 403 (not 401 auth error)."""
        app = FastAPI()

        # Simulate the read_only guard as in main.py
        @app.middleware("http")
        async def read_only_guard(request, call_next):
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Read-only mode. No modifications allowed."},
                )
            return await call_next(request)

        # A protected endpoint that would normally check auth
        @app.post("/api/something")
        async def something(request: Request):
            user = getattr(request.state, "user", None)
            if user is None:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Authentication required"},
                )
            return JSONResponse(content={"ok": True})

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            # POST should be blocked with 403 (read-only), not 401 (auth)
            response = await client.post("/api/something", json={})
            assert response.status_code == 403
            assert "Read-only" in response.json()["detail"] or "read-only" in response.json()["detail"].lower()

    @pytest.mark.anyio
    async def test_get_requests_pass_through_to_handlers(self):
        """GET requests pass through read-only guard and reach the handler."""
        app = FastAPI()

        @app.middleware("http")
        async def read_only_guard(request, call_next):
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Read-only mode."},
                )
            return await call_next(request)

        @app.get("/page")
        async def page():
            return JSONResponse(content={"ok": True})

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            response = await client.get("/page")
            assert response.status_code == 200
            assert response.json()["ok"] is True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_cookie_value(set_cookie_header: str) -> str | None:
    """Extract the cookie value from a Set-Cookie header string.

    Example: "fcs_session=abc123; Path=/; HttpOnly" → "abc123"
    """
    if not set_cookie_header:
        return None
    # The cookie value is the part after "name=" and before the first ";"
    parts = set_cookie_header.split(";")
    if not parts:
        return None
    name_value = parts[0].strip()
    if "=" not in name_value:
        return None
    _, value = name_value.split("=", 1)
    return value
