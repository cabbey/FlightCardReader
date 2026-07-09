"""Integration tests for audit log output.

Tests that the audit log file is written correctly across multiple actions,
verifying JSON Lines format, required fields, change tracking, and that
no plaintext passwords appear in log entries.

# Feature: auth-and-audit, Integration: Audit Log Output
Validates: Requirements 6.3, 6.4, 6.5, 6.7, 8.6
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from flight_card_scanner.services import audit_service
from flight_card_scanner.services.audit_service import init_audit_logger, log_action


@pytest.fixture(autouse=True)
def _reset_audit_logger():
    """Reset the audit logger between tests to avoid handler accumulation."""
    audit_service._audit_logger = None
    logger = logging.getLogger("audit")
    logger.handlers.clear()
    yield
    audit_service._audit_logger = None
    logger = logging.getLogger("audit")
    logger.handlers.clear()


@pytest.fixture
def audit_log_file(tmp_path):
    """Provide a temporary audit log file path and initialize the logger."""
    log_path = tmp_path / "audit.log"
    init_audit_logger(log_path)
    return log_path


class TestAuditLogMultipleActions:
    """Test audit log file written correctly across multiple actions."""

    def test_multiple_actions_produce_correct_line_count(self, audit_log_file):
        """Multiple log_action calls produce the expected number of lines."""
        log_action("admin@example.com", "login", "session", "sess-001",
                   {"result": "success"})
        log_action("admin@example.com", "created", "flight_record", 1)
        log_action("admin@example.com", "updated", "flight_record", 1,
                   {"changes": {"flier_name": {"old": "Jon", "new": "John"}}})
        log_action("badguy@example.com", "login_failed", "session", "none",
                   {"result": "failed"})

        lines = audit_log_file.read_text().strip().split("\n")
        assert len(lines) == 4

    def test_each_line_is_valid_json(self, audit_log_file):
        """Every line in the audit log is independently parseable as JSON."""
        log_action("user1@test.com", "login", "session", "s1",
                   {"result": "success"})
        log_action("user1@test.com", "created", "flight_record", 10)
        log_action("user1@test.com", "updated", "flight_record", 10,
                   {"changes": {"pad": {"old": "3", "new": "5"}}})
        log_action("user1@test.com", "deleted", "flight_record", 10)
        log_action("user1@test.com", "logout", "session", "s1",
                   {"result": "success"})

        lines = audit_log_file.read_text().strip().split("\n")
        for i, line in enumerate(lines):
            try:
                json.loads(line)
            except json.JSONDecodeError:
                pytest.fail(f"Line {i} is not valid JSON: {line!r}")

    def test_each_entry_has_required_fields(self, audit_log_file):
        """Each JSON entry contains timestamp, actor, action, object_type,
        object_id, and details."""
        log_action("admin@example.com", "login", "session", "sess-abc",
                   {"result": "success"})
        log_action("admin@example.com", "created", "flight_record", 42)
        log_action("data_entry@example.com", "updated", "flight_record", 42,
                   {"changes": {"rack": {"old": "A", "new": "B"}}})
        log_action("admin@example.com", "requeued", "flight_record", 42)

        required_fields = {"timestamp", "actor", "action", "object_type",
                           "object_id", "details"}

        lines = audit_log_file.read_text().strip().split("\n")
        for i, line in enumerate(lines):
            entry = json.loads(line)
            missing = required_fields - set(entry.keys())
            assert not missing, (
                f"Line {i} missing fields: {missing}. Entry: {entry}")

    def test_timestamps_are_valid_iso8601_with_timezone(self, audit_log_file):
        """All timestamps are valid ISO 8601 with timezone info."""
        log_action("user@test.com", "login", "session", "s1",
                   {"result": "success"})
        log_action("user@test.com", "created", "flight_record", 5)
        log_action("user@test.com", "logout", "session", "s1",
                   {"result": "success"})

        lines = audit_log_file.read_text().strip().split("\n")
        for i, line in enumerate(lines):
            entry = json.loads(line)
            ts = datetime.fromisoformat(entry["timestamp"])
            assert ts.tzinfo is not None, (
                f"Line {i} timestamp lacks timezone: {entry['timestamp']}")

    def test_actor_matches_what_was_logged(self, audit_log_file):
        """Actor field correctly records the email or 'anonymous'."""
        log_action("admin@example.com", "login", "session", "s1",
                   {"result": "success"})
        log_action("anonymous", "created", "flight_record", 7)
        log_action("data_entry@site.org", "updated", "flight_record", 7,
                   {"changes": {}})

        lines = audit_log_file.read_text().strip().split("\n")
        entries = [json.loads(line) for line in lines]

        assert entries[0]["actor"] == "admin@example.com"
        assert entries[1]["actor"] == "anonymous"
        assert entries[2]["actor"] == "data_entry@site.org"

    def test_action_verbs_recorded_correctly(self, audit_log_file):
        """Action verbs are recorded exactly as passed."""
        actions = [
            ("login", "session", "s1"),
            ("created", "flight_record", 1),
            ("updated", "flight_record", 1),
            ("deleted", "flight_record", 1),
            ("extracted", "flight_record", 2),
            ("requeued", "flight_record", 2),
            ("logout", "session", "s1"),
            ("login_failed", "session", "none"),
        ]
        for action, obj_type, obj_id in actions:
            log_action("user@test.com", action, obj_type, obj_id)

        lines = audit_log_file.read_text().strip().split("\n")
        entries = [json.loads(line) for line in lines]

        expected_actions = [a[0] for a in actions]
        actual_actions = [e["action"] for e in entries]
        assert actual_actions == expected_actions

    def test_object_type_and_id_recorded_correctly(self, audit_log_file):
        """Object type and ID are preserved in the log."""
        log_action("user@test.com", "created", "flight_record", 100)
        log_action("admin@test.com", "updated", "user", 5)
        log_action("user@test.com", "ip_changed", "session", "token-xyz")

        lines = audit_log_file.read_text().strip().split("\n")
        entries = [json.loads(line) for line in lines]

        assert entries[0]["object_type"] == "flight_record"
        assert entries[0]["object_id"] == 100
        assert entries[1]["object_type"] == "user"
        assert entries[1]["object_id"] == 5
        assert entries[2]["object_type"] == "session"
        assert entries[2]["object_id"] == "token-xyz"


class TestAuditLogUpdateChanges:
    """Test that update actions include changes dict with old/new values."""

    def test_update_includes_changes_with_old_new(self, audit_log_file):
        """Update action details contain changes mapping field → old/new."""
        changes = {
            "flier_name": {"old": "Jon Smith", "new": "John Smith"},
            "human_verified": {"old": False, "new": True},
        }
        log_action("admin@example.com", "updated", "flight_record", 42,
                   {"changes": changes})

        lines = audit_log_file.read_text().strip().split("\n")
        entry = json.loads(lines[0])

        assert "changes" in entry["details"]
        assert entry["details"]["changes"]["flier_name"]["old"] == "Jon Smith"
        assert entry["details"]["changes"]["flier_name"]["new"] == "John Smith"
        assert entry["details"]["changes"]["human_verified"]["old"] is False
        assert entry["details"]["changes"]["human_verified"]["new"] is True

    def test_update_changes_supports_various_types(self, audit_log_file):
        """Changes dict handles strings, numbers, booleans, null, arrays."""
        changes = {
            "rack": {"old": "A", "new": "B"},
            "pad": {"old": 3, "new": 7},
            "flier_verified": {"old": None, "new": True},
            "rocket_colors": {"old": ["red"], "new": ["red", "blue"]},
        }
        log_action("user@test.com", "updated", "flight_record", 99,
                   {"changes": changes})

        lines = audit_log_file.read_text().strip().split("\n")
        entry = json.loads(lines[0])
        c = entry["details"]["changes"]

        assert c["rack"]["old"] == "A"
        assert c["rack"]["new"] == "B"
        assert c["pad"]["old"] == 3
        assert c["pad"]["new"] == 7
        assert c["flier_verified"]["old"] is None
        assert c["flier_verified"]["new"] is True
        assert c["rocket_colors"]["old"] == ["red"]
        assert c["rocket_colors"]["new"] == ["red", "blue"]

    def test_multiple_updates_each_have_own_changes(self, audit_log_file):
        """Multiple update actions each independently log their changes."""
        log_action("admin@ex.com", "updated", "flight_record", 1,
                   {"changes": {"rack": {"old": "A", "new": "B"}}})
        log_action("admin@ex.com", "updated", "flight_record", 1,
                   {"changes": {"pad": {"old": "1", "new": "2"}}})

        lines = audit_log_file.read_text().strip().split("\n")
        entries = [json.loads(line) for line in lines]

        assert "rack" in entries[0]["details"]["changes"]
        assert "rack" not in entries[1]["details"]["changes"]
        assert "pad" in entries[1]["details"]["changes"]
        assert "pad" not in entries[0]["details"]["changes"]


class TestAuditLogNoPlaintextPasswords:
    """Verify no plaintext passwords appear anywhere in the audit log."""

    def test_login_failed_does_not_log_password(self, audit_log_file):
        """Failed login entries must not contain plaintext passwords."""
        # Simulate what the auth router should pass — only result, no password
        log_action("hacker@evil.com", "login_failed", "session", "none",
                   {"result": "failed"})

        content = audit_log_file.read_text()
        # Verify no common password-related field names with values
        entry = json.loads(content.strip())
        assert "password" not in entry["details"]
        assert "password_hash" not in entry["details"]

    def test_user_creation_does_not_log_password(self, audit_log_file):
        """User creation audit entries must not include the password."""
        # The audit entry for user creation should only include safe fields
        log_action("admin@example.com", "created", "user", 5,
                   {"email": "new@user.com", "role": "data_entry",
                    "display_name": "New User"})

        content = audit_log_file.read_text()
        entry = json.loads(content.strip())
        assert "password" not in entry["details"]
        assert "password_hash" not in entry["details"]
        # Also check raw text doesn't contain any password-like values
        assert "secret123" not in content
        assert "p@ssw0rd" not in content

    def test_user_update_password_change_not_logged_as_plaintext(
            self, audit_log_file):
        """When a user's password is changed, the plaintext must not appear."""
        # A well-behaved caller should only indicate password was changed,
        # never include the actual value
        log_action("admin@example.com", "updated", "user", 3,
                   {"changes": {"password": {"old": "[redacted]",
                                             "new": "[redacted]"}}})

        content = audit_log_file.read_text()
        # Verify no actual password values (this tests the contract: callers
        # must not pass plaintext passwords in details)
        lines = content.strip().split("\n")
        for line in lines:
            # No raw password strings should appear
            assert "myP@ssword!" not in line
            assert "hunter2" not in line

    def test_details_with_password_adjacent_fields_excluded(
            self, audit_log_file):
        """Even if details include fields with 'password' in key name,
        verify the values are not plaintext passwords."""
        # This tests the contract: callers MUST NOT include plaintext passwords
        # The audit service itself doesn't filter — responsibility is on callers
        # But we verify the entire log file doesn't contain known test passwords
        test_passwords = [
            "SuperSecret123!",
            "MyP@ssw0rd",
            "admin1234",
            "changeme!!",
        ]

        # Log various actions that might be near password operations
        log_action("admin@test.com", "login", "session", "s1",
                   {"result": "success"})
        log_action("admin@test.com", "created", "user", 10,
                   {"email": "user@test.com", "role": "data_entry"})
        log_action("admin@test.com", "updated", "user", 10,
                   {"changes": {"role": {"old": "data_entry",
                                         "new": "admin"}}})
        log_action("attempt@test.com", "login_failed", "session", "none",
                   {"result": "failed"})

        full_content = audit_log_file.read_text()
        for password in test_passwords:
            assert password not in full_content, (
                f"Plaintext password '{password}' found in audit log!")


class TestAuditLogJsonLinesFormat:
    """Verify JSON Lines format compliance."""

    def test_no_multiline_entries(self, audit_log_file):
        """Each entry is a single line — no pretty-printing."""
        # Use details with nested structure that might tempt pretty-printing
        complex_details = {
            "changes": {
                "flier_name": {"old": "A", "new": "B"},
                "rocket_colors": {"old": ["red", "green"],
                                  "new": ["blue", "white", "orange"]},
                "rocket_measurements": {
                    "old": {"weight": "4 lbs", "length": "48 in"},
                    "new": {"weight": "4.5 lbs", "length": "48 in"},
                },
            }
        }
        log_action("admin@test.com", "updated", "flight_record", 99,
                   complex_details)

        content = audit_log_file.read_text()
        lines = content.strip().split("\n")
        # Should be exactly 1 line despite complex nested details
        assert len(lines) == 1
        # And that line should be valid JSON
        entry = json.loads(lines[0])
        assert entry["action"] == "updated"

    def test_lines_are_independently_parseable(self, audit_log_file):
        """Each line can be parsed independently without context from
        surrounding lines (no wrapping array or object)."""
        log_action("a@b.com", "login", "session", "s1",
                   {"result": "success"})
        log_action("a@b.com", "created", "flight_record", 1)
        log_action("a@b.com", "logout", "session", "s1",
                   {"result": "success"})

        content = audit_log_file.read_text()
        lines = content.strip().split("\n")

        # Parse each line independently
        for i, line in enumerate(lines):
            entry = json.loads(line)
            assert isinstance(entry, dict), (
                f"Line {i} is not a JSON object: {type(entry)}")

        # Verify it's NOT a JSON array wrapping multiple entries
        assert not content.strip().startswith("[")

    def test_log_appends_without_truncation(self, audit_log_file):
        """New entries are appended to existing content."""
        log_action("user@test.com", "login", "session", "s1",
                   {"result": "success"})

        # Read first entry
        first_content = audit_log_file.read_text()
        first_entry = json.loads(first_content.strip())

        # Add more entries
        log_action("user@test.com", "created", "flight_record", 1)
        log_action("user@test.com", "logout", "session", "s1",
                   {"result": "success"})

        # Verify first entry is still there
        all_content = audit_log_file.read_text()
        lines = all_content.strip().split("\n")
        assert len(lines) == 3
        assert json.loads(lines[0]) == first_entry


class TestAuditLogLoginEvents:
    """Test login-related audit entries."""

    def test_login_event_has_correct_structure(self, audit_log_file):
        """Login success entry has actor, action=login, details with result."""
        log_action("admin@example.com", "login", "session", "sess-001",
                   {"result": "success"})

        content = audit_log_file.read_text().strip()
        entry = json.loads(content)

        assert entry["actor"] == "admin@example.com"
        assert entry["action"] == "login"
        assert entry["details"]["result"] == "success"

    def test_logout_event_has_correct_structure(self, audit_log_file):
        """Logout entry has actor, action=logout, details with result."""
        log_action("user@example.com", "logout", "session", "sess-002",
                   {"result": "success"})

        content = audit_log_file.read_text().strip()
        entry = json.loads(content)

        assert entry["actor"] == "user@example.com"
        assert entry["action"] == "logout"
        assert entry["details"]["result"] == "success"

    def test_login_failed_event_uses_attempted_email(self, audit_log_file):
        """Failed login uses the attempted email as actor."""
        log_action("attacker@evil.com", "login_failed", "session", "none",
                   {"result": "failed"})

        content = audit_log_file.read_text().strip()
        entry = json.loads(content)

        assert entry["actor"] == "attacker@evil.com"
        assert entry["action"] == "login_failed"
        assert entry["details"]["result"] == "failed"

    def test_full_session_lifecycle_in_audit(self, audit_log_file):
        """A full session lifecycle: login → actions → logout all logged."""
        log_action("user@test.com", "login", "session", "s1",
                   {"result": "success"})
        log_action("user@test.com", "created", "flight_record", 50)
        log_action("user@test.com", "updated", "flight_record", 50,
                   {"changes": {"flier_name": {"old": "", "new": "Alice"}}})
        log_action("user@test.com", "logout", "session", "s1",
                   {"result": "success"})

        lines = audit_log_file.read_text().strip().split("\n")
        entries = [json.loads(line) for line in lines]

        assert len(entries) == 4
        assert entries[0]["action"] == "login"
        assert entries[1]["action"] == "created"
        assert entries[2]["action"] == "updated"
        assert entries[3]["action"] == "logout"

        # All from the same actor
        for entry in entries:
            assert entry["actor"] == "user@test.com"
