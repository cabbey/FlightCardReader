"""Property-based test for audit log integrity.

# Feature: auth-and-audit, Property 7: Audit Log Integrity

**Validates: Requirements 6.3, 6.4, 6.5, 6.6, 8.6**

For any mutating action, the audit log SHALL contain exactly one new JSON line
that is independently parseable, contains a valid ISO 8601 timestamp with
timezone, the correct actor/action/object_type/object_id, and SHALL never
contain any plaintext password value.
"""

import json
import logging
import re
import tempfile
from datetime import datetime
from pathlib import Path

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from flight_card_scanner.services import audit_service
from flight_card_scanner.services.audit_service import init_audit_logger, log_action

# ---------------------------------------------------------------------------
# Valid value sets (from design doc)
# ---------------------------------------------------------------------------

VALID_ACTIONS = [
    "created",
    "updated",
    "deleted",
    "extracted",
    "requeued",
    "login",
    "logout",
    "login_failed",
    "ip_changed",
]

VALID_OBJECT_TYPES = [
    "flight_record",
    "user",
    "session",
]

# Common password-like patterns to check (requirement 8.6)
_PASSWORD_PATTERNS = [
    re.compile(r'"password"\s*:\s*"[^"]+"', re.IGNORECASE),
    re.compile(r'"passwd"\s*:\s*"[^"]+"', re.IGNORECASE),
    re.compile(r'"secret"\s*:\s*"[^"]+"', re.IGNORECASE),
    re.compile(r'"pwd"\s*:\s*"[^"]+"', re.IGNORECASE),
]

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# actor: any email-like string or "anonymous"
_email_strategy = st.emails()
_actor_strategy = st.one_of(
    _email_strategy,
    st.just("anonymous"),
)

# action: one of the valid verbs
_action_strategy = st.sampled_from(VALID_ACTIONS)

# object_type: one of the valid types
_object_type_strategy = st.sampled_from(VALID_OBJECT_TYPES)

# object_id: integer or string (session IDs are strings, record/user IDs are ints)
_object_id_strategy = st.one_of(
    st.integers(min_value=1, max_value=10_000_000),
    st.text(
        alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="-_"),
        min_size=1,
        max_size=64,
    ),
)

# details: optional dict — may have arbitrary keys/values but NEVER passwords.
# We use a conservative strategy: dicts with safe string keys and simple values.
_safe_key = st.text(
    alphabet=st.characters(whitelist_categories=("Ll",), whitelist_characters="_"),
    min_size=1,
    max_size=20,
).filter(lambda k: k not in ("password", "passwd", "secret", "pwd"))

_safe_value = st.one_of(
    st.text(max_size=50),
    st.integers(),
    st.booleans(),
    st.none(),
)

_details_strategy = st.one_of(
    st.none(),
    st.dictionaries(keys=_safe_key, values=_safe_value, max_size=5),
)


# ---------------------------------------------------------------------------
# Property 7: Audit Log Integrity
# ---------------------------------------------------------------------------


@given(
    actor=_actor_strategy,
    action=_action_strategy,
    object_type=_object_type_strategy,
    object_id=_object_id_strategy,
    details=_details_strategy,
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_audit_log_integrity(
    actor,
    action,
    object_type,
    object_id,
    details,
):
    """**Validates: Requirements 6.3, 6.4, 6.5, 6.6, 8.6**

    For any combination of valid (actor, action, object_type, object_id, details),
    calling log_action() SHALL produce exactly one new line in the log file that:
    - Is valid JSON
    - Has a valid ISO 8601 timestamp with timezone info
    - Contains the correct actor, action, object_type, and object_id
    - Contains no plaintext password values anywhere in the serialized entry
    """
    # Each hypothesis example needs its own fresh log file and logger.
    # We use tempfile directly since pytest fixtures don't reset per example.
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "audit_integrity.log"

        # Reset logger state before each example
        audit_service._audit_logger = None
        audit_logger = logging.getLogger("audit")
        audit_logger.handlers.clear()

        # Initialize with a fresh log file
        init_audit_logger(log_path)

        # Perform the action
        log_action(actor, action, object_type, object_id, details)

        # Flush handlers to ensure content is written
        for handler in audit_logger.handlers:
            handler.flush()

        # Read log after the action
        content = log_path.read_text()
        lines = content.splitlines()

        # --- Assert: exactly one line was written ---
        assert len(lines) == 1, (
            f"Expected exactly 1 audit line, got {len(lines)}: {lines!r}"
        )

        raw_line = lines[0]

        # --- Assert: line is valid JSON ---
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            pytest.fail(f"Audit line is not valid JSON: {exc}\nLine: {raw_line!r}")

        # --- Assert: timestamp is valid ISO 8601 with timezone ---
        assert "timestamp" in entry, "Audit entry missing 'timestamp' field"
        try:
            ts = datetime.fromisoformat(entry["timestamp"])
        except ValueError as exc:
            pytest.fail(
                f"Invalid ISO 8601 timestamp: {exc}\nValue: {entry['timestamp']!r}"
            )
        assert ts.tzinfo is not None, (
            f"Timestamp must be timezone-aware, got: {entry['timestamp']!r}"
        )

        # --- Assert: correct actor ---
        assert entry.get("actor") == actor, (
            f"Expected actor={actor!r}, got {entry.get('actor')!r}"
        )

        # --- Assert: correct action ---
        assert entry.get("action") == action, (
            f"Expected action={action!r}, got {entry.get('action')!r}"
        )

        # --- Assert: correct object_type ---
        assert entry.get("object_type") == object_type, (
            f"Expected object_type={object_type!r}, got {entry.get('object_type')!r}"
        )

        # --- Assert: correct object_id ---
        # json.dumps/loads preserves int vs string distinction
        assert entry.get("object_id") == object_id, (
            f"Expected object_id={object_id!r}, got {entry.get('object_id')!r}"
        )

        # --- Assert: no plaintext passwords anywhere in the serialized JSON ---
        for pattern in _PASSWORD_PATTERNS:
            assert not pattern.search(raw_line), (
                f"Audit line appears to contain a plaintext password "
                f"(matched pattern {pattern.pattern!r}):\n{raw_line!r}"
            )

        # Clean up logger for next example
        audit_service._audit_logger = None
        audit_logger.handlers.clear()
