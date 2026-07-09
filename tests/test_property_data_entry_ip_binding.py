# Feature: auth-and-audit, Property 12: Data Entry Soft IP Binding
"""
Property-based test for data entry soft IP binding.

For any data_entry session created with a recorded client IP, when a subsequent
request arrives from a different IP address, the session SHALL remain valid
(request proceeds normally) AND the audit log SHALL contain an "ip_changed"
event with the session ID, old IP, and new IP.

**Validates: Requirements 8.9, 6.6**
"""

import json
import logging
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from flight_card_scanner.auth_models import AuthBase, User, Session
from flight_card_scanner.services import audit_service
from flight_card_scanner.services.audit_service import init_audit_logger
from flight_card_scanner.services.auth_service import AuthService

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Generate IPv4 addresses as strings
_ipv4_strategy = st.tuples(
    st.integers(min_value=1, max_value=255),
    st.integers(min_value=0, max_value=255),
    st.integers(min_value=0, max_value=255),
    st.integers(min_value=1, max_value=254),
).map(lambda t: f"{t[0]}.{t[1]}.{t[2]}.{t[3]}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_in_memory_session_factory() -> async_sessionmaker[AsyncSession]:
    """Create an in-memory SQLite async session factory with auth tables."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(AuthBase.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Property 12: Data Entry Soft IP Binding
# ---------------------------------------------------------------------------


class TestDataEntrySoftIPBinding:
    """Property 12: Data Entry Soft IP Binding.

    For any data_entry session created with a recorded client IP, when a
    subsequent request arrives from a different IP address, the session SHALL
    remain valid and the audit log SHALL contain an "ip_changed" event.
    """

    @given(original_ip=_ipv4_strategy, new_ip=_ipv4_strategy)
    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @pytest.mark.asyncio
    async def test_session_remains_valid_on_ip_change(
        self, original_ip: str, new_ip: str
    ):
        """A data_entry session survives IP changes (soft binding)."""
        assume(original_ip != new_ip)

        # Set up in-memory auth database
        session_factory = await _create_in_memory_session_factory()

        # Set up fresh audit logger for this example
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audit.log"

            # Reset audit logger state
            audit_service._audit_logger = None
            audit_logger = logging.getLogger("audit")
            audit_logger.handlers.clear()
            init_audit_logger(log_path)

            # Create AuthService
            auth = AuthService(
                session_factory=session_factory,
                session_secret="test-secret-at-least-16-chars",
                timeout_hours=8.0,
            )

            # Create a data_entry user
            user = await auth.create_user(
                email="dataentry@example.com",
                display_name="Data Entry User",
                password="securepassword123",
                role="data_entry",
            )

            # Create session with original IP
            token = await auth.create_session(
                user_id=user.id, client_ip=original_ip
            )

            # Validate session with a different IP — should return the user
            result = await auth.validate_session(token, client_ip=new_ip)

            # Session MUST remain valid
            assert result is not None, (
                f"data_entry session should remain valid on IP change "
                f"(original={original_ip}, new={new_ip})"
            )
            assert result.id == user.id
            assert result.email == "dataentry@example.com"
            assert result.role == "data_entry"

            # Clean up logger
            audit_service._audit_logger = None
            audit_logger.handlers.clear()

    @given(original_ip=_ipv4_strategy, new_ip=_ipv4_strategy)
    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @pytest.mark.asyncio
    async def test_ip_changed_audit_entry_written(
        self, original_ip: str, new_ip: str
    ):
        """An 'ip_changed' audit entry is written when IP changes for data_entry."""
        assume(original_ip != new_ip)

        # Set up in-memory auth database
        session_factory = await _create_in_memory_session_factory()

        # Set up fresh audit logger for this example
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audit.log"

            # Reset audit logger state
            audit_service._audit_logger = None
            audit_logger = logging.getLogger("audit")
            audit_logger.handlers.clear()
            init_audit_logger(log_path)

            # Create AuthService
            auth = AuthService(
                session_factory=session_factory,
                session_secret="test-secret-at-least-16-chars",
                timeout_hours=8.0,
            )

            # Create a data_entry user
            user = await auth.create_user(
                email="dataentry@example.com",
                display_name="Data Entry User",
                password="securepassword123",
                role="data_entry",
            )

            # Create session with original IP
            token = await auth.create_session(
                user_id=user.id, client_ip=original_ip
            )

            # Validate session with a different IP
            await auth.validate_session(token, client_ip=new_ip)

            # Flush handlers to ensure content is written
            for handler in audit_logger.handlers:
                handler.flush()

            # Read the audit log
            content = log_path.read_text()
            lines = content.splitlines()

            # There MUST be at least one audit entry
            assert len(lines) >= 1, (
                f"Expected at least 1 audit entry for ip_changed, got {len(lines)}"
            )

            # Find the ip_changed entry
            ip_changed_entries = []
            for line in lines:
                entry = json.loads(line)
                if entry.get("action") == "ip_changed":
                    ip_changed_entries.append(entry)

            # Exactly one ip_changed entry
            assert len(ip_changed_entries) == 1, (
                f"Expected exactly 1 ip_changed entry, got {len(ip_changed_entries)}"
            )

            entry = ip_changed_entries[0]

            # Verify the entry fields
            assert entry["actor"] == "dataentry@example.com"
            assert entry["action"] == "ip_changed"
            assert entry["object_type"] == "session"
            assert entry["object_id"] == token
            assert entry["details"]["old_ip"] == original_ip
            assert entry["details"]["new_ip"] == new_ip

            # Clean up logger
            audit_service._audit_logger = None
            audit_logger.handlers.clear()
