# Feature: auth-and-audit, Property 11: Admin Strict IP Binding
"""
Property-based test for admin strict IP binding.

For any admin session created with a recorded client IP, when a subsequent
request arrives from a different IP address, the session SHALL be immediately
invalidated and the request treated as unauthenticated.

**Validates: Requirements 8.8**
"""

import asyncio

import pytest
from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from flight_card_scanner.auth_models import AuthBase, User, Session
from flight_card_scanner.services.auth_service import AuthService


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Generate IPv4 addresses as strings (e.g., "192.168.1.1")
_ipv4_strategy = st.tuples(
    st.integers(min_value=0, max_value=255),
    st.integers(min_value=0, max_value=255),
    st.integers(min_value=0, max_value=255),
    st.integers(min_value=0, max_value=255),
).map(lambda t: f"{t[0]}.{t[1]}.{t[2]}.{t[3]}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _setup_auth_service():
    """Create an in-memory SQLite auth database and return an AuthService."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    # Create tables using the auth_models AuthBase metadata
    async with engine.begin() as conn:
        await conn.run_sync(AuthBase.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    auth_service = AuthService(
        session_factory=session_factory,
        session_secret="test-secret-for-property-test",
        timeout_hours=8.0,
    )

    return auth_service, engine


# ---------------------------------------------------------------------------
# Property 11: Admin Strict IP Binding
# ---------------------------------------------------------------------------


class TestAdminStrictIPBinding:
    """Property 11: Admin Strict IP Binding.

    For any admin session created with a recorded client IP, when a subsequent
    request arrives from a different IP address, the session SHALL be immediately
    invalidated and the request treated as unauthenticated.
    """

    @given(original_ip=_ipv4_strategy, different_ip=_ipv4_strategy)
    @settings(
        max_examples=100,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_different_ip_invalidates_admin_session(
        self, original_ip: str, different_ip: str
    ):
        """An admin session validated from a different IP returns None (invalidated).

        **Validates: Requirements 8.8**
        """
        assume(original_ip != different_ip)

        async def _run():
            auth_service, engine = await _setup_auth_service()

            try:
                # Create an admin user
                user = await auth_service.create_user(
                    email="admin@test.example",
                    display_name="Test Admin",
                    password="securepassword123",
                    role="admin",
                )

                # Create a session with the original IP
                token = await auth_service.create_session(
                    user_id=user.id, client_ip=original_ip
                )

                # Validate the session with a different IP — should return None
                result = await auth_service.validate_session(
                    token=token, client_ip=different_ip
                )

                assert result is None, (
                    f"Expected None (session invalidated) when admin session "
                    f"created with IP {original_ip!r} is validated from "
                    f"IP {different_ip!r}, but got user {result!r}"
                )
            finally:
                await engine.dispose()

        asyncio.run(_run())

    @given(ip=_ipv4_strategy)
    @settings(
        max_examples=100,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_same_ip_keeps_admin_session_valid(self, ip: str):
        """An admin session validated from the same IP remains valid.

        **Validates: Requirements 8.8**
        """

        async def _run():
            auth_service, engine = await _setup_auth_service()

            try:
                # Create an admin user
                user = await auth_service.create_user(
                    email="admin@test.example",
                    display_name="Test Admin",
                    password="securepassword123",
                    role="admin",
                )

                # Create a session with the IP
                token = await auth_service.create_session(
                    user_id=user.id, client_ip=ip
                )

                # Validate the session with the same IP — should return the user
                result = await auth_service.validate_session(
                    token=token, client_ip=ip
                )

                assert result is not None, (
                    f"Expected user to be returned when admin session is "
                    f"validated from the same IP {ip!r}, but got None"
                )
                assert result.id == user.id
                assert result.role == "admin"
            finally:
                await engine.dispose()

        asyncio.run(_run())
