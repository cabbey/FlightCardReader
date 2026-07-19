# Feature: auth-and-audit, Property 5: User CRUD Correctness
"""
Property-based test for user CRUD correctness.

For any valid user creation request (email <= 254 chars, display_name <= 100
chars, password 8-128 chars, role in {"admin", "data_entry"}), creating the
user and then retrieving it SHALL return the same email (lowercased),
display_name, and role, with a non-plaintext password_hash and active=True.

**Validates: Requirements 5.2, 5.3**
"""

import asyncio

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from flight_card_scanner.auth_models import AuthBase
from flight_card_scanner.services.auth_service import AuthService


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Valid email: 1-254 chars, contains @ with local and domain parts.
# We generate simple but valid-looking emails to avoid DB issues.
_email_local_strategy = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789._+-"),
    min_size=1,
    max_size=64,
)
_email_domain_strategy = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789-"),
    min_size=1,
    max_size=30,
).filter(lambda s: not s.startswith("-") and not s.endswith("-"))

_email_strategy = st.builds(
    lambda local, domain, tld: f"{local}@{domain}.{tld}",
    local=_email_local_strategy,
    domain=_email_domain_strategy,
    tld=st.sampled_from(["com", "org", "net", "io", "dev", "example"]),
).filter(lambda e: len(e) <= 254)

# Add some mixed-case emails to verify lowercasing
_mixed_case_email_strategy = _email_strategy.map(
    lambda e: "".join(
        c.upper() if i % 2 == 0 else c.lower() for i, c in enumerate(e)
    )
).filter(lambda e: len(e) <= 254)

_final_email_strategy = st.one_of(_email_strategy, _mixed_case_email_strategy)

# Display name: 1-100 chars, printable text
_display_name_strategy = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P", "Z"),
        blacklist_characters="\x00",
    ),
    min_size=1,
    max_size=100,
).filter(lambda s: s.strip())  # Ensure non-empty after strip

# Password: 8-128 chars, printable
_password_strategy = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P", "S"),
        blacklist_characters="\x00",
    ),
    min_size=8,
    max_size=128,
)

# Role: one of the two valid roles
_role_strategy = st.sampled_from(["admin", "data_entry"])


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
        session_secret="test-secret-for-property-test",
        timeout_hours=8.0,
    )

    return auth_service, engine


# ---------------------------------------------------------------------------
# Property 5: User CRUD Correctness
# ---------------------------------------------------------------------------


class TestUserCRUDCorrectness:
    """Property 5: User CRUD Correctness.

    For any valid user creation request, creating the user and then
    retrieving it SHALL return the same email (lowercased), display_name,
    and role, with a non-plaintext password_hash and active=True.
    """

    @given(
        email=_final_email_strategy,
        display_name=_display_name_strategy,
        password=_password_strategy,
        role=_role_strategy,
    )
    @settings(
        max_examples=100,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_user_creation_round_trip(
        self, email: str, display_name: str, password: str, role: str
    ):
        """Created user has correct email (lowercased), display_name, role,
        active=True, and non-plaintext password_hash.

        **Validates: Requirements 5.2, 5.3**
        """

        async def _run():
            auth_service, engine = await _setup_auth_service()

            try:
                # Create the user
                user = await auth_service.create_user(
                    email=email,
                    display_name=display_name,
                    password=password,
                    role=role,
                )

                # Verify email is stored as lowercase/stripped
                assert user.email == email.lower().strip(), (
                    f"Expected email {email.lower().strip()!r}, "
                    f"got {user.email!r}"
                )

                # Verify display_name is preserved exactly
                assert user.display_name == display_name, (
                    f"Expected display_name {display_name!r}, "
                    f"got {user.display_name!r}"
                )

                # Verify role is preserved exactly
                assert user.role == role, (
                    f"Expected role {role!r}, got {user.role!r}"
                )

                # Verify active defaults to True
                assert user.active is True, (
                    f"Expected active=True, got {user.active!r}"
                )

                # Verify password_hash starts with "$argon2id$"
                assert user.password_hash.startswith("$argon2id$"), (
                    f"Expected password_hash to start with '$argon2id$', "
                    f"got {user.password_hash[:20]!r}..."
                )

                # Verify password_hash is NOT the plaintext password
                assert user.password_hash != password, (
                    "password_hash must not be the plaintext password"
                )
            finally:
                await engine.dispose()

        asyncio.run(_run())
