# Feature: auth-and-audit, Property 13: Hard Max Lifetime Enforcement
"""
Property-based test for hard max lifetime enforcement.

For any session, regardless of activity, the session SHALL be invalid once the
elapsed time since `created_at` exceeds the role-specific Hard_Max_Lifetime
(8 hours for admin, 120 hours for data_entry). Active use (updating `last_active`)
SHALL NOT extend the session beyond this hard limit.

**Validates: Requirements 2.12, 2.13, 8.10**
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st
from sqlalchemy import update
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from flight_card_scanner.auth_models import AuthBase, Session
from flight_card_scanner.services.auth_service import AuthService, HARD_MAX_LIFETIME_HOURS


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Session age in hours as floats — covering both under and over the limit
# Admin max is 8h, data_entry max is 120h. We generate ages from 0 to 130h
# to cover both roles' boundaries.
_admin_age_hours = st.floats(min_value=0.0, max_value=20.0, allow_nan=False, allow_infinity=False)
_data_entry_age_hours = st.floats(min_value=0.0, max_value=250.0, allow_nan=False, allow_infinity=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _setup_auth_service():
    """Create an in-memory SQLite auth database and return an AuthService.

    Uses a very long idle timeout (1000h) so idle timeout doesn't interfere
    with hard max lifetime testing.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(AuthBase.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    auth_service = AuthService(
        session_factory=session_factory,
        session_secret="test-secret-for-hard-max-lifetime",
        timeout_hours=1000.0,  # Very long idle timeout so it doesn't interfere
    )

    return auth_service, engine, session_factory


async def _age_session(session_factory, token: str, age_hours: float):
    """Manipulate a session's created_at to simulate aging by the given hours.

    Also updates last_active to be recent (simulating active use) so that
    idle timeout doesn't expire the session.
    """
    now = datetime.now(timezone.utc)
    new_created_at = now - timedelta(hours=age_hours)

    async with session_factory() as db:
        await db.execute(
            update(Session)
            .where(Session.id == token)
            .values(
                created_at=new_created_at,
                # Keep last_active recent so idle timeout doesn't interfere
                last_active=now - timedelta(seconds=1),
            )
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Property 13: Hard Max Lifetime Enforcement — Admin (8h)
# ---------------------------------------------------------------------------


class TestHardMaxLifetimeAdmin:
    """Property 13: Hard Max Lifetime Enforcement for admin role.

    Admin sessions have a hard max lifetime of 8 hours.
    """

    @given(age_hours=_admin_age_hours)
    @settings(
        max_examples=100,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_admin_session_validity_based_on_age(self, age_hours: float):
        """Admin session valid if age < 8h, invalid if age > 8h.

        **Validates: Requirements 2.12, 2.13, 8.10**
        """
        max_lifetime = HARD_MAX_LIFETIME_HOURS["admin"]  # 8 hours

        # Avoid testing exactly at the boundary (floating point edge)
        assume(abs(age_hours - max_lifetime) > 0.001)

        async def _run():
            auth_service, engine, session_factory = await _setup_auth_service()

            try:
                # Create an admin user
                user = await auth_service.create_user(
                    email="admin@test.example",
                    display_name="Test Admin",
                    password="securepassword123",
                    role="admin",
                )

                # Create a session
                token = await auth_service.create_session(
                    user_id=user.id, client_ip="127.0.0.1"
                )

                # Age the session by manipulating created_at
                await _age_session(session_factory, token, age_hours)

                # Validate the session
                result = await auth_service.validate_session(
                    token=token, client_ip="127.0.0.1"
                )

                if age_hours < max_lifetime:
                    assert result is not None, (
                        f"Expected admin session aged {age_hours:.3f}h "
                        f"(< {max_lifetime}h hard max) to be valid, but got None"
                    )
                    assert result.id == user.id
                else:
                    assert result is None, (
                        f"Expected admin session aged {age_hours:.3f}h "
                        f"(> {max_lifetime}h hard max) to be invalid, "
                        f"but got user {result!r}"
                    )
            finally:
                await engine.dispose()

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Property 13: Hard Max Lifetime Enforcement — Data Entry (120h)
# ---------------------------------------------------------------------------


class TestHardMaxLifetimeDataEntry:
    """Property 13: Hard Max Lifetime Enforcement for data_entry role.

    Data entry sessions have a hard max lifetime of 120 hours (5 days).
    """

    @given(age_hours=_data_entry_age_hours)
    @settings(
        max_examples=100,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_data_entry_session_validity_based_on_age(self, age_hours: float):
        """Data entry session valid if age < 120h, invalid if age > 120h.

        **Validates: Requirements 2.12, 2.13, 8.10**
        """
        max_lifetime = HARD_MAX_LIFETIME_HOURS["data_entry"]  # 120 hours

        # Avoid testing exactly at the boundary (floating point edge)
        assume(abs(age_hours - max_lifetime) > 0.001)

        async def _run():
            auth_service, engine, session_factory = await _setup_auth_service()

            try:
                # Create a data_entry user
                user = await auth_service.create_user(
                    email="dataentry@test.example",
                    display_name="Test Data Entry",
                    password="securepassword123",
                    role="data_entry",
                )

                # Create a session
                token = await auth_service.create_session(
                    user_id=user.id, client_ip="127.0.0.1"
                )

                # Age the session by manipulating created_at
                await _age_session(session_factory, token, age_hours)

                # Validate the session
                result = await auth_service.validate_session(
                    token=token, client_ip="127.0.0.1"
                )

                if age_hours < max_lifetime:
                    assert result is not None, (
                        f"Expected data_entry session aged {age_hours:.3f}h "
                        f"(< {max_lifetime}h hard max) to be valid, but got None"
                    )
                    assert result.id == user.id
                else:
                    assert result is None, (
                        f"Expected data_entry session aged {age_hours:.3f}h "
                        f"(> {max_lifetime}h hard max) to be invalid, "
                        f"but got user {result!r}"
                    )
            finally:
                await engine.dispose()

        asyncio.run(_run())
