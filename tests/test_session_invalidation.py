# Feature: auth-and-audit, Property 6: Session Invalidation on Deactivation
"""
Unit tests for session invalidation on user deactivation.

For any user with N active sessions (N >= 1), deactivating that user SHALL
invalidate all N sessions such that subsequent requests using any of those
session tokens are treated as unauthenticated.

**Validates: Requirements 5.5**
"""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from flight_card_scanner.auth_models import AuthBase
from flight_card_scanner.services.auth_service import AuthService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _setup_auth_service():
    """Create an in-memory SQLite auth database and return an AuthService."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(AuthBase.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    auth_service = AuthService(
        session_factory=session_factory,
        session_secret="test-secret-for-session-invalidation",
        timeout_hours=8.0,
    )

    return auth_service, engine


# ---------------------------------------------------------------------------
# Property 6: Session Invalidation on Deactivation
# ---------------------------------------------------------------------------


class TestSessionInvalidationOnDeactivation:
    """Property 6: Session Invalidation on Deactivation.

    For any user with N active sessions (N >= 1), calling
    invalidate_user_sessions(user_id) SHALL invalidate all N sessions such that
    validate_session returns None for each.
    """

    @pytest.mark.parametrize("num_sessions", [1, 2, 5])
    def test_all_sessions_invalidated(self, num_sessions: int):
        """Create user with N sessions, invalidate all, verify each returns None.

        **Validates: Requirements 5.5**
        """

        async def _run():
            auth_service, engine = await _setup_auth_service()

            try:
                # Create a user
                user = await auth_service.create_user(
                    email="user@test.example",
                    display_name="Test User",
                    password="securepassword123",
                    role="data_entry",
                )

                # Create N sessions
                tokens = []
                for i in range(num_sessions):
                    token = await auth_service.create_session(
                        user_id=user.id, client_ip=f"10.0.0.{i + 1}"
                    )
                    tokens.append(token)

                # Verify all sessions are valid before invalidation
                for token in tokens:
                    result = await auth_service.validate_session(token=token)
                    assert result is not None, (
                        f"Session {token!r} should be valid before invalidation"
                    )
                    assert result.id == user.id

                # Invalidate all sessions for the user
                await auth_service.invalidate_user_sessions(user.id)

                # Verify ALL sessions are now invalid
                for token in tokens:
                    result = await auth_service.validate_session(token=token)
                    assert result is None, (
                        f"Session {token!r} should be None after "
                        f"invalidate_user_sessions, but got user {result!r}"
                    )
            finally:
                await engine.dispose()

        asyncio.run(_run())

    def test_other_users_sessions_not_affected(self):
        """Invalidating one user's sessions does NOT affect other users' sessions.

        **Validates: Requirements 5.5**
        """

        async def _run():
            auth_service, engine = await _setup_auth_service()

            try:
                # Create two users
                user_a = await auth_service.create_user(
                    email="usera@test.example",
                    display_name="User A",
                    password="securepassword123",
                    role="data_entry",
                )
                user_b = await auth_service.create_user(
                    email="userb@test.example",
                    display_name="User B",
                    password="securepassword456",
                    role="data_entry",
                )

                # Create sessions for both users
                token_a1 = await auth_service.create_session(
                    user_id=user_a.id, client_ip="10.0.0.1"
                )
                token_a2 = await auth_service.create_session(
                    user_id=user_a.id, client_ip="10.0.0.2"
                )
                token_b1 = await auth_service.create_session(
                    user_id=user_b.id, client_ip="10.0.0.3"
                )
                token_b2 = await auth_service.create_session(
                    user_id=user_b.id, client_ip="10.0.0.4"
                )

                # Invalidate only User A's sessions
                await auth_service.invalidate_user_sessions(user_a.id)

                # User A's sessions should be invalid
                assert await auth_service.validate_session(token=token_a1) is None
                assert await auth_service.validate_session(token=token_a2) is None

                # User B's sessions should STILL be valid
                result_b1 = await auth_service.validate_session(token=token_b1)
                assert result_b1 is not None, (
                    "User B's session should remain valid after "
                    "invalidating User A's sessions"
                )
                assert result_b1.id == user_b.id

                result_b2 = await auth_service.validate_session(token=token_b2)
                assert result_b2 is not None, (
                    "User B's second session should remain valid after "
                    "invalidating User A's sessions"
                )
                assert result_b2.id == user_b.id
            finally:
                await engine.dispose()

        asyncio.run(_run())

    def test_invalidation_is_idempotent(self):
        """Calling invalidate_user_sessions twice does not raise an error.

        **Validates: Requirements 5.5**
        """

        async def _run():
            auth_service, engine = await _setup_auth_service()

            try:
                # Create a user with a session
                user = await auth_service.create_user(
                    email="user@test.example",
                    display_name="Test User",
                    password="securepassword123",
                    role="admin",
                )
                token = await auth_service.create_session(
                    user_id=user.id, client_ip="192.168.1.1"
                )

                # Invalidate once
                await auth_service.invalidate_user_sessions(user.id)
                assert await auth_service.validate_session(token=token) is None

                # Invalidate again — should not raise
                await auth_service.invalidate_user_sessions(user.id)
                assert await auth_service.validate_session(token=token) is None
            finally:
                await engine.dispose()

        asyncio.run(_run())

    def test_invalidation_with_no_sessions(self):
        """Calling invalidate_user_sessions on a user with no sessions does not raise.

        **Validates: Requirements 5.5**
        """

        async def _run():
            auth_service, engine = await _setup_auth_service()

            try:
                # Create a user without any sessions
                user = await auth_service.create_user(
                    email="nosessions@test.example",
                    display_name="No Sessions",
                    password="securepassword123",
                    role="data_entry",
                )

                # Should not raise
                await auth_service.invalidate_user_sessions(user.id)
            finally:
                await engine.dispose()

        asyncio.run(_run())
