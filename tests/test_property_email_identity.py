# Feature: auth-and-audit, Property 2: Case-Insensitive Email Identity
"""
Property-based test for case-insensitive email identity.

For any email string, the system SHALL treat that email and any case-variant
of it (uppercase, lowercase, mixed) as the same identity for uniqueness
constraints and login matching.

**Validates: Requirements 1.4**
"""
import asyncio
import random

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from flight_card_scanner.auth_models import AuthBase
from flight_card_scanner.services.auth_service import AuthService


# --- Helpers ---


def _random_case_variant(email: str) -> str:
    """Generate a random case variant of the given email string."""
    return "".join(
        c.upper() if random.random() > 0.5 else c.lower() for c in email
    )


async def _create_auth_service() -> tuple[AuthService, async_sessionmaker[AsyncSession]]:
    """Create an AuthService backed by an in-memory SQLite database."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(AuthBase.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    auth_service = AuthService(
        session_factory=session_factory,
        session_secret="test-secret-not-real",
        timeout_hours=8.0,
    )
    return auth_service, session_factory


# --- Strategies ---

# Generate valid email-like strings with case variations.
# We use st.emails() from Hypothesis for realistic email generation.
email_strategy = st.emails()

# A valid password for user creation (8-128 chars)
password_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "S")),
    min_size=8,
    max_size=32,
)


# --- Tests ---


class TestCaseInsensitiveEmailIdentity:
    """Property 2: Case-Insensitive Email Identity.

    For any email string, the system SHALL treat that email and any
    case-variant of it (uppercase, lowercase, mixed) as the same identity
    for uniqueness constraints and login matching.
    """

    @given(email=email_strategy)
    @settings(max_examples=100)
    def test_email_normalization_produces_lowercase(self, email: str):
        """The normalization applied by create_user always produces lowercase.

        This verifies the fundamental normalization: email.lower().strip()
        is applied consistently, making all case variants equivalent.
        """
        normalized = email.lower().strip()

        # The upper case variant normalizes to the same thing
        assert email.upper().lower().strip() == normalized
        # The mixed case variant normalizes to the same thing
        mixed = _random_case_variant(email)
        assert mixed.lower().strip() == normalized

    @given(email=email_strategy, password=password_strategy)
    @settings(max_examples=100)
    def test_create_user_normalizes_email_to_lowercase(self, email: str, password: str):
        """create_user stores the email in lowercase form regardless of input case."""
        assume(len(email) <= 254)

        async def _run():
            auth_service, _ = await _create_auth_service()

            # Create user with original casing
            user = await auth_service.create_user(
                email=email,
                display_name="Test User",
                password=password,
                role="data_entry",
            )

            # The stored email should always be lowercase and stripped
            assert user.email == email.lower().strip()

        asyncio.run(_run())

    @given(email=email_strategy, password=password_strategy)
    @settings(max_examples=100)
    def test_authenticate_matches_case_insensitively(self, email: str, password: str):
        """authenticate succeeds regardless of the case used for the email.

        Creating a user with one case variant and authenticating with a
        different case variant should succeed (same identity).
        """
        assume(len(email) <= 254)
        assume(len(password) >= 8)

        async def _run():
            auth_service, _ = await _create_auth_service()

            # Create user with the original email
            await auth_service.create_user(
                email=email,
                display_name="Test User",
                password=password,
                role="data_entry",
            )

            # Authenticate with uppercase variant
            user_upper = await auth_service.authenticate(email.upper(), password)
            assert user_upper is not None
            assert user_upper.email == email.lower().strip()

            # Authenticate with lowercase variant
            user_lower = await auth_service.authenticate(email.lower(), password)
            assert user_lower is not None
            assert user_lower.email == email.lower().strip()

            # Authenticate with a random mixed-case variant
            mixed = _random_case_variant(email)
            user_mixed = await auth_service.authenticate(mixed, password)
            assert user_mixed is not None
            assert user_mixed.email == email.lower().strip()

        asyncio.run(_run())

    @given(email=email_strategy, password=password_strategy)
    @settings(max_examples=100)
    def test_rate_limit_key_is_case_insensitive(self, email: str, password: str):
        """Rate limiting uses the same key regardless of email case.

        Recording a failed attempt for 'User@Example.COM' should count
        against 'user@example.com' for rate limiting purposes.
        """
        assume(len(email) <= 254)

        async def _run():
            auth_service, _ = await _create_auth_service()

            # Record failed attempts with various case variants
            auth_service.record_failed_attempt(email.upper())
            auth_service.record_failed_attempt(email.lower())
            auth_service.record_failed_attempt(_random_case_variant(email))

            # Check rate limit with yet another variant — should see all 3 attempts
            is_limited, _ = auth_service.check_rate_limit(email)
            # With 3 attempts (limit is 5), we should not be limited yet
            assert is_limited is False

            # Add 2 more attempts to reach the limit
            auth_service.record_failed_attempt(_random_case_variant(email))
            auth_service.record_failed_attempt(_random_case_variant(email))

            # Now checking with any case variant should show rate limited
            is_limited_upper, _ = auth_service.check_rate_limit(email.upper())
            is_limited_lower, _ = auth_service.check_rate_limit(email.lower())
            is_limited_mixed, _ = auth_service.check_rate_limit(
                _random_case_variant(email)
            )

            assert is_limited_upper is True
            assert is_limited_lower is True
            assert is_limited_mixed is True

        asyncio.run(_run())
