"""Unit tests for the session middleware.

Tests cookie signing/unsigning, session resolution, user attachment to
request.state, and cookie clearing on invalid sessions.

Validates: Requirements 2.4, 2.6, 2.8, 2.9
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from itsdangerous import URLSafeSerializer
from starlette.requests import Request
from starlette.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from flight_card_scanner.middleware.session_middleware import SessionMiddleware


# ---------------------------------------------------------------------------
# Test Helpers
# ---------------------------------------------------------------------------

SESSION_SECRET = "test-secret-at-least-16-chars"
COOKIE_NAME = "fcs_session"


def _make_serializer():
    """Create a serializer matching the middleware's internal one."""
    return URLSafeSerializer(SESSION_SECRET, salt="fcs-session")


def _create_mock_auth_service(user=None):
    """Create a mock AuthService that returns the given user on validate_session."""
    auth_service = AsyncMock()
    auth_service.validate_session = AsyncMock(return_value=user)
    return auth_service


def _create_test_app(auth_service, secure=False):
    """Create a minimal Starlette app wrapped with SessionMiddleware."""

    async def homepage(request):
        user = getattr(request.state, "user", "NOT_SET")
        token = getattr(request.state, "session_token", "NOT_SET")
        clear = getattr(request.state, "clear_session_cookie", "NOT_SET")
        return JSONResponse({
            "user_id": user.id if user else None,
            "user_email": user.email if user else None,
            "session_token": token,
            "clear_session_cookie": clear,
        })

    app = Starlette(routes=[Route("/", homepage)])
    app = SessionMiddleware(
        app=app,
        auth_service=auth_service,
        cookie_name=COOKIE_NAME,
        session_secret=SESSION_SECRET,
        secure=secure,
    )
    return app


# ---------------------------------------------------------------------------
# Tests: Cookie Signing/Unsigning
# ---------------------------------------------------------------------------


class TestCookieSigning:
    """Test that the middleware correctly signs and unsigns cookies."""

    def test_sign_token_produces_valid_signed_value(self):
        """sign_token should produce a value that can be unsigned."""
        auth_service = _create_mock_auth_service()
        mw = SessionMiddleware(
            app=AsyncMock(),
            auth_service=auth_service,
            session_secret=SESSION_SECRET,
        )
        token = "abc123-session-token"
        signed = mw.sign_token(token)

        # Should be different from the raw token
        assert signed != token

        # Should be unsignable back to the original
        serializer = _make_serializer()
        assert serializer.loads(signed) == token

    def test_invalid_signature_returns_no_token(self):
        """A cookie with an invalid signature should be treated as no session."""
        auth_service = _create_mock_auth_service()
        app = _create_test_app(auth_service)
        client = TestClient(app)

        # Send a cookie with a tampered/invalid value
        response = client.get("/", cookies={COOKIE_NAME: "tampered-value"})
        data = response.json()

        # Should treat as unauthenticated (no token extracted)
        assert data["session_token"] is None
        assert data["user_id"] is None
        # validate_session should NOT be called since no valid token
        auth_service.validate_session.assert_not_called()

    def test_empty_cookie_returns_no_token(self):
        """An empty cookie value should be treated as no session."""
        auth_service = _create_mock_auth_service()
        app = _create_test_app(auth_service)
        client = TestClient(app)

        response = client.get("/", cookies={COOKIE_NAME: ""})
        data = response.json()

        assert data["session_token"] is None
        assert data["user_id"] is None
        auth_service.validate_session.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Session Resolution
# ---------------------------------------------------------------------------


class TestSessionResolution:
    """Test session validation and user attachment to request.state."""

    def test_valid_session_attaches_user(self):
        """A valid signed cookie with a valid session should attach the user."""
        mock_user = MagicMock()
        mock_user.id = 42
        mock_user.email = "test@example.com"
        auth_service = _create_mock_auth_service(user=mock_user)
        app = _create_test_app(auth_service)
        client = TestClient(app)

        # Sign a token
        serializer = _make_serializer()
        signed_cookie = serializer.dumps("valid-token-123")

        response = client.get("/", cookies={COOKIE_NAME: signed_cookie})
        data = response.json()

        assert data["user_id"] == 42
        assert data["user_email"] == "test@example.com"
        assert data["session_token"] == "valid-token-123"
        assert data["clear_session_cookie"] is False

        # validate_session should have been called with the token
        auth_service.validate_session.assert_called_once()
        call_args = auth_service.validate_session.call_args
        # Token is passed as first positional arg
        assert call_args[0][0] == "valid-token-123"

    def test_invalid_session_sets_clear_flag(self):
        """A valid signed cookie but invalid session should set clear_session_cookie."""
        # validate_session returns None (session expired/invalid)
        auth_service = _create_mock_auth_service(user=None)
        app = _create_test_app(auth_service)
        client = TestClient(app)

        serializer = _make_serializer()
        signed_cookie = serializer.dumps("expired-token")

        response = client.get("/", cookies={COOKIE_NAME: signed_cookie})
        data = response.json()

        assert data["user_id"] is None
        assert data["clear_session_cookie"] is True
        assert data["session_token"] == "expired-token"

    def test_no_cookie_sets_user_none(self):
        """No session cookie means user is None and no clear needed."""
        auth_service = _create_mock_auth_service()
        app = _create_test_app(auth_service)
        client = TestClient(app)

        response = client.get("/")
        data = response.json()

        assert data["user_id"] is None
        assert data["session_token"] is None
        assert data["clear_session_cookie"] is False

    def test_client_ip_passed_to_validate_session(self):
        """The client IP should be forwarded to validate_session."""
        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.email = "user@example.com"
        auth_service = _create_mock_auth_service(user=mock_user)
        app = _create_test_app(auth_service)
        client = TestClient(app)

        serializer = _make_serializer()
        signed_cookie = serializer.dumps("some-token")

        response = client.get("/", cookies={COOKIE_NAME: signed_cookie})

        # validate_session should have been called with client_ip
        auth_service.validate_session.assert_called_once()
        call_kwargs = auth_service.validate_session.call_args
        # The client_ip should be present (testclient uses "testclient")
        assert "client_ip" in call_kwargs.kwargs or len(call_kwargs.args) >= 2


# ---------------------------------------------------------------------------
# Tests: Cookie Clearing
# ---------------------------------------------------------------------------


class TestCookieClearing:
    """Test that invalid sessions trigger cookie clearing in the response."""

    def test_clear_cookie_header_on_invalid_session(self):
        """When session is invalid, response should include Set-Cookie header clearing it."""
        auth_service = _create_mock_auth_service(user=None)
        app = _create_test_app(auth_service)
        client = TestClient(app)

        serializer = _make_serializer()
        signed_cookie = serializer.dumps("invalid-token")

        response = client.get("/", cookies={COOKIE_NAME: signed_cookie})

        # Check that Set-Cookie header is present to clear the cookie
        set_cookie_headers = response.headers.getlist("set-cookie") if hasattr(response.headers, 'getlist') else [
            v for k, v in response.headers.multi_items() if k.lower() == "set-cookie"
        ]
        assert len(set_cookie_headers) > 0, "Expected Set-Cookie header to clear cookie"

        # Find the clearing cookie
        clear_header = None
        for header in set_cookie_headers:
            if COOKIE_NAME in header and "Max-Age=0" in header:
                clear_header = header
                break

        assert clear_header is not None, (
            f"Expected Set-Cookie header with Max-Age=0 for {COOKIE_NAME}, "
            f"got headers: {set_cookie_headers}"
        )
        assert "HttpOnly" in clear_header
        assert "SameSite=Lax" in clear_header
        assert "Path=/" in clear_header

    def test_no_clear_cookie_on_valid_session(self):
        """When session is valid, no clearing Set-Cookie header should be present."""
        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.email = "user@example.com"
        auth_service = _create_mock_auth_service(user=mock_user)
        app = _create_test_app(auth_service)
        client = TestClient(app)

        serializer = _make_serializer()
        signed_cookie = serializer.dumps("valid-token")

        response = client.get("/", cookies={COOKIE_NAME: signed_cookie})

        # Should NOT have a clearing Set-Cookie header for our cookie
        set_cookie_headers = [
            v for k, v in response.headers.multi_items() if k.lower() == "set-cookie"
        ]
        clearing_headers = [
            h for h in set_cookie_headers
            if COOKIE_NAME in h and "Max-Age=0" in h
        ]
        assert len(clearing_headers) == 0

    def test_no_clear_cookie_when_no_cookie_sent(self):
        """When no cookie is sent at all, no clearing should happen."""
        auth_service = _create_mock_auth_service()
        app = _create_test_app(auth_service)
        client = TestClient(app)

        response = client.get("/")

        set_cookie_headers = [
            v for k, v in response.headers.multi_items() if k.lower() == "set-cookie"
        ]
        clearing_headers = [
            h for h in set_cookie_headers
            if COOKIE_NAME in h and "Max-Age=0" in h
        ]
        assert len(clearing_headers) == 0


# ---------------------------------------------------------------------------
# Tests: Cookie Attributes
# ---------------------------------------------------------------------------


class TestCookieAttributes:
    """Test cookie attribute settings (HttpOnly, SameSite, Secure)."""

    def test_clear_cookie_has_httponly(self):
        """Clearing cookie should have HttpOnly attribute."""
        auth_service = _create_mock_auth_service(user=None)
        app = _create_test_app(auth_service, secure=False)
        client = TestClient(app)

        serializer = _make_serializer()
        signed_cookie = serializer.dumps("expired-token")

        response = client.get("/", cookies={COOKIE_NAME: signed_cookie})
        set_cookie_headers = [
            v for k, v in response.headers.multi_items() if k.lower() == "set-cookie"
        ]
        clear_header = next(
            (h for h in set_cookie_headers if COOKIE_NAME in h), None
        )
        assert clear_header is not None
        assert "HttpOnly" in clear_header

    def test_clear_cookie_has_samesite_lax(self):
        """Clearing cookie should have SameSite=Lax attribute."""
        auth_service = _create_mock_auth_service(user=None)
        app = _create_test_app(auth_service, secure=False)
        client = TestClient(app)

        serializer = _make_serializer()
        signed_cookie = serializer.dumps("expired-token")

        response = client.get("/", cookies={COOKIE_NAME: signed_cookie})
        set_cookie_headers = [
            v for k, v in response.headers.multi_items() if k.lower() == "set-cookie"
        ]
        clear_header = next(
            (h for h in set_cookie_headers if COOKIE_NAME in h), None
        )
        assert clear_header is not None
        assert "SameSite=Lax" in clear_header

    def test_clear_cookie_has_secure_when_ssl_configured(self):
        """Clearing cookie should have Secure flag when SSL is configured."""
        auth_service = _create_mock_auth_service(user=None)
        app = _create_test_app(auth_service, secure=True)
        client = TestClient(app)

        serializer = _make_serializer()
        signed_cookie = serializer.dumps("expired-token")

        response = client.get("/", cookies={COOKIE_NAME: signed_cookie})
        set_cookie_headers = [
            v for k, v in response.headers.multi_items() if k.lower() == "set-cookie"
        ]
        clear_header = next(
            (h for h in set_cookie_headers if COOKIE_NAME in h), None
        )
        assert clear_header is not None
        assert "Secure" in clear_header

    def test_clear_cookie_no_secure_when_ssl_not_configured(self):
        """Clearing cookie should NOT have Secure flag when SSL is not configured."""
        auth_service = _create_mock_auth_service(user=None)
        app = _create_test_app(auth_service, secure=False)
        client = TestClient(app)

        serializer = _make_serializer()
        signed_cookie = serializer.dumps("expired-token")

        response = client.get("/", cookies={COOKIE_NAME: signed_cookie})
        set_cookie_headers = [
            v for k, v in response.headers.multi_items() if k.lower() == "set-cookie"
        ]
        clear_header = next(
            (h for h in set_cookie_headers if COOKIE_NAME in h), None
        )
        assert clear_header is not None
        assert "Secure" not in clear_header

    def test_build_set_cookie_header_attributes(self):
        """build_set_cookie_header should include all required attributes."""
        auth_service = _create_mock_auth_service()
        mw = SessionMiddleware(
            app=AsyncMock(),
            auth_service=auth_service,
            session_secret=SESSION_SECRET,
            secure=True,
        )

        header = mw.build_set_cookie_header("signed-value-here")
        assert "fcs_session=signed-value-here" in header
        assert "HttpOnly" in header
        assert "SameSite=Lax" in header
        assert "Path=/" in header
        assert "Secure" in header

    def test_build_set_cookie_header_no_secure_when_not_configured(self):
        """build_set_cookie_header without secure should omit Secure flag."""
        auth_service = _create_mock_auth_service()
        mw = SessionMiddleware(
            app=AsyncMock(),
            auth_service=auth_service,
            session_secret=SESSION_SECRET,
            secure=False,
        )

        header = mw.build_set_cookie_header("signed-value-here")
        assert "Secure" not in header
        assert "HttpOnly" in header
        assert "SameSite=Lax" in header

    def test_build_set_cookie_header_with_max_age(self):
        """build_set_cookie_header with max_age should include Max-Age."""
        auth_service = _create_mock_auth_service()
        mw = SessionMiddleware(
            app=AsyncMock(),
            auth_service=auth_service,
            session_secret=SESSION_SECRET,
            secure=False,
        )

        header = mw.build_set_cookie_header("token", max_age=3600)
        assert "Max-Age=3600" in header


# ---------------------------------------------------------------------------
# Tests: Non-HTTP scopes pass through
# ---------------------------------------------------------------------------


class TestNonHTTPPassthrough:
    """Test that non-HTTP ASGI scopes (websocket, lifespan) pass through unchanged."""

    def test_websocket_scope_passes_through(self):
        """Non-HTTP scopes should be passed directly to the inner app."""
        inner_app = AsyncMock()
        auth_service = _create_mock_auth_service()
        mw = SessionMiddleware(
            app=inner_app,
            auth_service=auth_service,
            session_secret=SESSION_SECRET,
        )

        scope = {"type": "websocket"}
        receive = AsyncMock()
        send = AsyncMock()

        asyncio.run(mw(scope, receive, send))

        inner_app.assert_called_once_with(scope, receive, send)
        auth_service.validate_session.assert_not_called()
