"""Authentication service — pure business logic, no HTTP concerns.

Handles user creation, password verification, session lifecycle, IP binding,
and rate limiting. Uses argon2id for password hashing and secrets.token_urlsafe
for session IDs.
"""

import secrets
import time
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flight_card_scanner.auth_models import Session, User
from flight_card_scanner.services.audit_service import log_action

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RATE_LIMIT_MAX_ATTEMPTS = 5
RATE_LIMIT_WINDOW_SECONDS = 15 * 60  # 15 minutes

# Hard max lifetime per role (not configurable)
HARD_MAX_LIFETIME_HOURS = {
    "admin": 8,
    "data_entry": 120,
}

# Argon2id hasher with default (secure) parameters
_hasher = PasswordHasher()

# Dummy hash used for timing-safe authentication when user doesn't exist.
# Pre-computed once so we don't waste time hashing at import but it's cheap
# enough to just define a constant that looks like a real hash.
_DUMMY_HASH: str | None = None


def _get_dummy_hash() -> str:
    """Return a pre-computed argon2id hash for timing-safe comparison."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = _hasher.hash("dummy_password_for_timing_safety")
    return _DUMMY_HASH


# ---------------------------------------------------------------------------
# AuthService
# ---------------------------------------------------------------------------


class AuthService:
    """Core authentication and session management service."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        session_secret: str,
        timeout_hours: float,
    ) -> None:
        """Initialize the auth service.

        Args:
            session_factory: SQLAlchemy async session factory for the auth DB.
            session_secret: Secret key for cookie signing (not used here
                directly — middleware handles cookie signing).
            timeout_hours: Idle timeout in hours for sessions.
        """
        self._session_factory = session_factory
        self._session_secret = session_secret
        self._timeout_hours = timeout_hours

        # In-memory rate limiting storage (resets on restart)
        self._failed_attempts: dict[str, list[float]] = {}

    # ------------------------------------------------------------------
    # User management
    # ------------------------------------------------------------------

    async def create_user(
        self,
        email: str,
        display_name: str,
        password: str,
        role: str,
    ) -> User:
        """Create a new user with an argon2id-hashed password.

        Args:
            email: User email (will be normalized to lowercase).
            display_name: Display name for the user.
            password: Plaintext password to hash.
            role: One of "admin" or "data_entry".

        Returns:
            The newly created User object.
        """
        normalized_email = email.lower().strip()
        password_hash = _hasher.hash(password)

        user = User(
            email=normalized_email,
            display_name=display_name,
            password_hash=password_hash,
            role=role,
            active=True,
        )

        async with self._session_factory() as db:
            db.add(user)
            await db.commit()
            await db.refresh(user)

        return user

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self, email: str, password: str) -> User | None:
        """Verify credentials. Returns User or None.

        Always runs argon2 verify (even for non-existent emails) to prevent
        timing attacks that could reveal whether an account exists.

        Args:
            email: The email address to authenticate.
            password: The plaintext password to verify.

        Returns:
            The authenticated User if credentials are valid and account is
            active, otherwise None.
        """
        normalized_email = email.lower().strip()

        async with self._session_factory() as db:
            result = await db.execute(
                select(User).where(User.email == normalized_email)
            )
            user = result.scalar_one_or_none()

        if user is None:
            # Timing-safe: always run argon2 verify even for non-existent users
            try:
                _hasher.verify(_get_dummy_hash(), password)
            except VerifyMismatchError:
                pass
            return None

        if not user.active:
            # Timing-safe: still verify password for inactive users
            try:
                _hasher.verify(user.password_hash, password)
            except VerifyMismatchError:
                pass
            return None

        try:
            _hasher.verify(user.password_hash, password)
        except VerifyMismatchError:
            return None

        # Check if the hash needs rehashing (parameter upgrade)
        if _hasher.check_needs_rehash(user.password_hash):
            new_hash = _hasher.hash(password)
            async with self._session_factory() as db:
                await db.execute(
                    update(User)
                    .where(User.id == user.id)
                    .values(password_hash=new_hash)
                )
                await db.commit()

        return user

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def create_session(
        self, user_id: int, client_ip: str | None = None
    ) -> str:
        """Generate a session token, store in DB with client_ip, return token.

        Args:
            user_id: The ID of the authenticated user.
            client_ip: The client IP address at session creation.

        Returns:
            The generated session token (secrets.token_urlsafe(32)).
        """
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)

        session = Session(
            id=token,
            user_id=user_id,
            created_at=now,
            last_active=now,
            is_valid=True,
            client_ip=client_ip,
        )

        async with self._session_factory() as db:
            db.add(session)
            await db.commit()

        return token

    async def validate_session(
        self, token: str, client_ip: str | None = None
    ) -> User | None:
        """Look up session, check expiry and IP binding, update last_active.

        Checks:
        1. Session exists and is_valid
        2. Idle timeout (last_active + timeout_hours)
        3. Hard max lifetime (created_at + role-specific limit)
        4. IP binding:
           - admin: strict (invalidate on IP change)
           - data_entry: soft (log "ip_changed", continue)

        Args:
            token: The session token from the cookie.
            client_ip: The current request's client IP.

        Returns:
            The User if session is valid, otherwise None.
        """
        async with self._session_factory() as db:
            result = await db.execute(
                select(Session).where(
                    Session.id == token, Session.is_valid == True  # noqa: E712
                )
            )
            session = result.scalar_one_or_none()

            if session is None:
                return None

            now = datetime.now(timezone.utc)

            # Check idle timeout
            idle_expiry = session.last_active + timedelta(
                hours=self._timeout_hours
            )
            if now > idle_expiry:
                # Session expired due to inactivity
                session.is_valid = False
                await db.commit()
                return None

            # Load the user
            user_result = await db.execute(
                select(User).where(
                    User.id == session.user_id, User.active == True  # noqa: E712
                )
            )
            user = user_result.scalar_one_or_none()

            if user is None:
                # User no longer exists or is deactivated
                session.is_valid = False
                await db.commit()
                return None

            # Check Hard_Max_Lifetime based on role
            max_hours = HARD_MAX_LIFETIME_HOURS.get(user.role, 8)
            hard_expiry = session.created_at + timedelta(hours=max_hours)
            if now > hard_expiry:
                # Session exceeded hard max lifetime
                session.is_valid = False
                await db.commit()
                return None

            # IP binding enforcement
            if client_ip is not None and session.client_ip is not None:
                if client_ip != session.client_ip:
                    if user.role == "admin":
                        # Strict IP binding for admin: invalidate session
                        session.is_valid = False
                        await db.commit()
                        return None
                    else:
                        # Soft IP binding for data_entry: log and continue
                        log_action(
                            actor=user.email,
                            action="ip_changed",
                            object_type="session",
                            object_id=session.id,
                            details={
                                "old_ip": session.client_ip,
                                "new_ip": client_ip,
                            },
                        )

            # Update last_active
            session.last_active = now
            await db.commit()

            return user

    async def invalidate_session(self, token: str) -> None:
        """Mark a session as invalid.

        Args:
            token: The session token to invalidate.
        """
        async with self._session_factory() as db:
            await db.execute(
                update(Session)
                .where(Session.id == token)
                .values(is_valid=False)
            )
            await db.commit()

    async def invalidate_user_sessions(self, user_id: int) -> None:
        """Invalidate all sessions for a user (e.g., on deactivation).

        Args:
            user_id: The user whose sessions should be invalidated.
        """
        async with self._session_factory() as db:
            await db.execute(
                update(Session)
                .where(
                    Session.user_id == user_id,
                    Session.is_valid == True,  # noqa: E712
                )
                .values(is_valid=False)
            )
            await db.commit()

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def check_rate_limit(self, email: str) -> tuple[bool, int]:
        """Check if an email address is rate-limited.

        Uses an in-memory sliding window of 5 attempts per 15 minutes.

        Args:
            email: The email address to check.

        Returns:
            A tuple of (is_limited, seconds_remaining). If is_limited is True,
            seconds_remaining indicates how long until the oldest attempt
            expires from the window.
        """
        key = email.lower().strip()
        now = time.time()
        attempts = self._failed_attempts.get(key, [])
        # Prune attempts outside the window
        attempts = [
            t for t in attempts if now - t < RATE_LIMIT_WINDOW_SECONDS
        ]
        self._failed_attempts[key] = attempts

        if len(attempts) >= RATE_LIMIT_MAX_ATTEMPTS:
            # Seconds until the oldest attempt expires
            oldest = min(attempts)
            remaining = int(RATE_LIMIT_WINDOW_SECONDS - (now - oldest)) + 1
            return (True, remaining)
        return (False, 0)

    def record_failed_attempt(self, email: str) -> None:
        """Record a failed login attempt for rate limiting.

        Args:
            email: The email address that had a failed attempt.
        """
        key = email.lower().strip()
        now = time.time()
        if key not in self._failed_attempts:
            self._failed_attempts[key] = []
        self._failed_attempts[key].append(now)

    def reset_failed_attempts(self, email: str) -> None:
        """Clear failed attempts on successful login.

        Args:
            email: The email address to clear attempts for.
        """
        key = email.lower().strip()
        self._failed_attempts.pop(key, None)
