# Feature: auth-and-audit, Property 3: Session Lifecycle Validity
"""
Property-based test for session lifecycle validity.

For any authenticated user with an active session, the session SHALL be valid
for requests made within the idle timeout period AND within the role-specific
Hard_Max_Lifetime. After the idle timeout elapses without activity, OR after
the Hard_Max_Lifetime is exceeded regardless of activity, the session SHALL
be treated as expired and the user as unauthenticated.

**Validates: Requirements 2.2, 2.7, 2.8, 2.9, 2.12, 2.13**
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
# Constants
# ---------------------------------------------------------------------------

# Idle timeout used for testing (4 hours, as specified in task guidance)
TEST_IDLE_TIMEOUT_HOURS = 4.0

# Hard max lifetimes per role
ADMIN_MAX_HOURS = HARD_MAX_LIFETIME_HOURS["admin"]  # 8
DATA_ENTRY_MAX_HOURS = HARD_MAX_LIFETIME_HOURS["data_entry"]  # 120


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Role strategy
_role = st.sampled_from(["admin", "data_entry"])

# Session age in hours (time since creation)
# Cover range 0 to 130h to span both admin (8h) and data_entry (120h) boundaries
_session_age_hours = st.floats(min_value=0.0, max_value=130.0, allow_nan=False, allow_infinity=False)

# Idle time in hours (time since last activity)
# Cover range 0 to 10h to span idle timeout boundary (4h)
_idle_time_hours = st.floats(min_value=0.0, max_value=10.0, allow_nan=False, allow_infinity=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _setup_auth_service():
    """Create an in-memory SQLite auth database and return an AuthService.

    Uses TEST_IDLE_TIMEOUT_HOURS (4h) as the idle timeout.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(AuthBase.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    auth_service = AuthService(
        session_factory=session_factory,
        session_secret="test-secret-for-session-lifecycle",
        timeout_hours=TEST_IDLE_TIMEOUT_HOURS,
    )

    return auth_service, engine, session_factory


async def _manipulate_session_timestamps(
    session_factory, token: str, age_hours: float, idle_hours: float
):
    """Manipulate a session's created_at and last_active to simulate
    a session that was created `age_hours` ago and last active `idle_hours` ago.
    """
    now = datetime.now(timezone.utc)
    new_created_at = now - timedelta(hours=age_hours)
    new_last_active = now - timedelta(hours=idle_hours)

    async with session_factory() as db:
        await db.execute(
            update(Session)
            .where(Session.id == token)
            .values(
                created_at=new_created_at,
                last_active=new_last_active,
            )
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Property 3: Session Lifecycle Validity
# ---------------------------------------------------------------------------


class TestSessionLifecycleValidity:
    """Property 3: Session Lifecycle Validity.

    A session is valid iff:
      idle_time < idle_timeout AND session_age < Hard_Max_Lifetime

    A session is expired if:
      idle_time >= idle_timeout OR session_age >= Hard_Max_Lifetime
    """

    @given(role=_role, age_hours=_session_age_hours, idle_hours=_idle_time_hours)
    @settings(
        max_examples=200,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_session_validity_determined_by_idle_and_hard_max(
        self, role: str, age_hours: float, idle_hours: float
    ):
        """Session is valid when both idle time < timeout AND age < hard max.
        Session is invalid when either limit is exceeded.

        **Validates: Requirements 2.2, 2.7, 2.8, 2.9, 2.12, 2.13**
        """
        hard_max = HARD_MAX_LIFETIME_HOURS[role]

        # Avoid floating point boundary ambiguity — stay away from exact boundaries
        assume(abs(idle_hours - TEST_IDLE_TIMEOUT_HOURS) > 0.001)
        assume(abs(age_hours - hard_max) > 0.001)

        # idle_time must be <= age (can't be idle longer than the session existed)
        assume(idle_hours <= age_hours + 0.001)

        # Determine expected validity
        within_idle = idle_hours < TEST_IDLE_TIMEOUT_HOURS
        within_hard_max = age_hours < hard_max
        expected_valid = within_idle and within_hard_max

        async def _run():
            auth_service, engine, session_factory = await _setup_auth_service()
            client_ip = "192.168.1.100"

            try:
                # Create a user with the given role
                user = await auth_service.create_user(
                    email=f"{role}@lifecycle-test.example",
                    display_name=f"Test {role}",
                    password="securepassword123",
                    role=role,
                )

                # Create a session with same IP (avoid IP binding interference)
                token = await auth_service.create_session(
                    user_id=user.id, client_ip=client_ip
                )

                # Manipulate timestamps to simulate the given age and idle time
                await _manipulate_session_timestamps(
                    session_factory, token, age_hours, idle_hours
                )

                # Validate the session using same IP
                result = await auth_service.validate_session(
                    token=token, client_ip=client_ip
                )

                if expected_valid:
                    assert result is not None, (
                        f"Expected {role} session (age={age_hours:.3f}h, "
                        f"idle={idle_hours:.3f}h) to be VALID "
                        f"(idle_timeout={TEST_IDLE_TIMEOUT_HOURS}h, "
                        f"hard_max={hard_max}h), but got None"
                    )
                    assert result.id == user.id
                else:
                    assert result is None, (
                        f"Expected {role} session (age={age_hours:.3f}h, "
                        f"idle={idle_hours:.3f}h) to be EXPIRED "
                        f"(idle_timeout={TEST_IDLE_TIMEOUT_HOURS}h, "
                        f"hard_max={hard_max}h), but session was still valid"
                    )
            finally:
                await engine.dispose()

        asyncio.run(_run())
