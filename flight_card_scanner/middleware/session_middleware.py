"""Session middleware — resolves signed session cookie and attaches user to request.state.

ASGI middleware that:
1. Reads the session cookie from the request
2. Unsigns it using itsdangerous
3. Validates the session via AuthService
4. Attaches the user (or None) to request.state
5. On response, clears the cookie if the session was invalid/expired
"""

import logging
from typing import Any

from itsdangerous import BadSignature, URLSafeSerializer
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from flight_card_scanner.services.auth_service import AuthService

logger = logging.getLogger(__name__)


class SessionMiddleware:
    """ASGI middleware that resolves the session cookie and attaches user to request.state."""

    def __init__(
        self,
        app: ASGIApp,
        auth_service: AuthService,
        cookie_name: str = "fcs_session",
        session_secret: str = "",
        secure: bool = False,
    ) -> None:
        """Initialize the session middleware.

        Args:
            app: The ASGI application to wrap.
            auth_service: AuthService instance for session validation.
            cookie_name: Name of the session cookie.
            session_secret: Secret key for signing/unsigning the cookie value.
            secure: Whether to set the Secure flag on cookies (True if SSL configured).
        """
        self.app = app
        self.auth_service = auth_service
        self.cookie_name = cookie_name
        self.session_secret = session_secret
        self.secure = secure
        self._serializer = URLSafeSerializer(session_secret, salt="fcs-session")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Process HTTP requests; pass through non-HTTP scopes unchanged."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)

        # Decode signed cookie to get session token
        token = self._get_session_token(request)
        user = None
        clear_cookie = False

        if token:
            client_ip = request.client.host if request.client else None
            user = await self.auth_service.validate_session(
                token, client_ip=client_ip
            )
            if user is None:
                # Session is invalid/expired/IP-rejected — schedule cookie clearing
                clear_cookie = True

        # Attach user (or None) and session token to request state
        scope.setdefault("state", {})
        scope["state"]["user"] = user
        scope["state"]["session_token"] = token
        scope["state"]["clear_session_cookie"] = clear_cookie

        if clear_cookie:
            # Intercept response to add Set-Cookie header that clears the cookie
            await self.app(scope, receive, self._make_clear_cookie_send(send))
        else:
            await self.app(scope, receive, send)

    def _get_session_token(self, request: Request) -> str | None:
        """Extract and unsign the session token from the cookie.

        Returns the raw session token (DB lookup key) or None if the cookie
        is missing, empty, or has an invalid signature.
        """
        signed_value = request.cookies.get(self.cookie_name)
        if not signed_value:
            return None

        try:
            token = self._serializer.loads(signed_value)
            return token
        except BadSignature:
            logger.debug("Invalid session cookie signature")
            return None

    def sign_token(self, token: str) -> str:
        """Sign a session token for setting in a cookie.

        This is a helper used by login/logout handlers to create properly
        signed cookie values.

        Args:
            token: The raw session token (from AuthService.create_session).

        Returns:
            The signed token string suitable for cookie value.
        """
        return self._serializer.dumps(token)

    def _make_clear_cookie_send(self, original_send: Send) -> Send:
        """Create a send wrapper that injects a Set-Cookie header to clear the session cookie."""

        async def send_with_clear_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                # Build the Set-Cookie header to clear the cookie
                clear_cookie_value = self._build_clear_cookie_header()
                headers.append(
                    (b"set-cookie", clear_cookie_value.encode("latin-1"))
                )
                message = {**message, "headers": headers}
            await original_send(message)

        return send_with_clear_cookie

    def _build_clear_cookie_header(self) -> str:
        """Build a Set-Cookie header string that clears the session cookie."""
        parts = [
            f"{self.cookie_name}=",
            "Path=/",
            "Max-Age=0",
            "HttpOnly",
            "SameSite=Lax",
        ]
        if self.secure:
            parts.append("Secure")
        return "; ".join(parts)

    def build_set_cookie_header(self, signed_token: str, max_age: int | None = None) -> str:
        """Build a Set-Cookie header string for setting the session cookie.

        Args:
            signed_token: The signed session token value.
            max_age: Optional max-age in seconds. If None, creates a session cookie.

        Returns:
            The complete Set-Cookie header value string.
        """
        parts = [
            f"{self.cookie_name}={signed_token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
        ]
        if max_age is not None:
            parts.append(f"Max-Age={max_age}")
        if self.secure:
            parts.append("Secure")
        return "; ".join(parts)
