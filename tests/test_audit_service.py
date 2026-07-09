"""Unit tests for the audit service.

Validates: Requirements 6.1, 6.2, 6.3, 6.7, 6.8, 6.9, 6.10, 8.6
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
    """Reset the module-level audit logger between tests."""
    audit_service._audit_logger = None
    # Also clean up any handlers attached to the "audit" logger
    logger = logging.getLogger("audit")
    logger.handlers.clear()
    yield
    audit_service._audit_logger = None
    logger.handlers.clear()


@pytest.fixture
def audit_log_path(tmp_path):
    """Provide a temporary audit log file path."""
    return tmp_path / "audit.log"


class TestInitAuditLogger:
    """Tests for init_audit_logger()."""

    def test_creates_logger_with_correct_name(self, audit_log_path):
        init_audit_logger(audit_log_path)
        assert audit_service._audit_logger is not None
        assert audit_service._audit_logger.name == "audit"

    def test_logger_does_not_propagate(self, audit_log_path):
        init_audit_logger(audit_log_path)
        assert audit_service._audit_logger.propagate is False

    def test_logger_has_file_handler(self, audit_log_path):
        init_audit_logger(audit_log_path)
        handlers = audit_service._audit_logger.handlers
        assert len(handlers) >= 1
        assert any(isinstance(h, logging.FileHandler) for h in handlers)

    def test_file_handler_uses_append_mode(self, audit_log_path):
        init_audit_logger(audit_log_path)
        handler = audit_service._audit_logger.handlers[0]
        assert handler.mode == "a"

    def test_creates_parent_directory_if_needed(self, tmp_path):
        nested = tmp_path / "sub" / "dir" / "audit.log"
        init_audit_logger(nested)
        assert nested.parent.exists()

    def test_does_not_truncate_existing_file(self, audit_log_path):
        # Write pre-existing content
        audit_log_path.write_text("existing line\n")
        init_audit_logger(audit_log_path)
        log_action("admin@test.com", "login", "session", "sess1")
        content = audit_log_path.read_text()
        assert "existing line" in content

    def test_logger_level_is_info(self, audit_log_path):
        init_audit_logger(audit_log_path)
        assert audit_service._audit_logger.level == logging.INFO


class TestLogAction:
    """Tests for log_action()."""

    def test_writes_json_line(self, audit_log_path):
        init_audit_logger(audit_log_path)
        log_action("user@example.com", "created", "flight_record", 42)
        content = audit_log_path.read_text().strip()
        entry = json.loads(content)
        assert entry["actor"] == "user@example.com"
        assert entry["action"] == "created"
        assert entry["object_type"] == "flight_record"
        assert entry["object_id"] == 42

    def test_includes_valid_iso_timestamp(self, audit_log_path):
        init_audit_logger(audit_log_path)
        log_action("user@example.com", "login", "session", "s1")
        content = audit_log_path.read_text().strip()
        entry = json.loads(content)
        # Should parse as a valid ISO 8601 datetime
        ts = datetime.fromisoformat(entry["timestamp"])
        assert ts.tzinfo is not None  # timezone-aware

    def test_details_defaults_to_empty_dict(self, audit_log_path):
        init_audit_logger(audit_log_path)
        log_action("user@example.com", "deleted", "flight_record", 10)
        content = audit_log_path.read_text().strip()
        entry = json.loads(content)
        assert entry["details"] == {}

    def test_includes_details_when_provided(self, audit_log_path):
        init_audit_logger(audit_log_path)
        details = {"changes": {"flier_name": {"old": "Jon", "new": "John"}}}
        log_action("admin@test.com", "updated", "flight_record", 5, details)
        content = audit_log_path.read_text().strip()
        entry = json.loads(content)
        assert entry["details"]["changes"]["flier_name"]["new"] == "John"

    def test_json_lines_format_one_per_line(self, audit_log_path):
        init_audit_logger(audit_log_path)
        log_action("a@b.com", "login", "session", "s1")
        log_action("c@d.com", "logout", "session", "s2")
        lines = audit_log_path.read_text().strip().split("\n")
        assert len(lines) == 2
        # Each line is independently parseable JSON
        for line in lines:
            json.loads(line)

    def test_does_not_raise_when_logger_not_initialized(self):
        """log_action should be safe to call even before init."""
        # Should not raise
        log_action("user@test.com", "login", "session", "s1")

    def test_does_not_raise_on_write_failure(self, tmp_path, caplog):
        """Fire-and-forget: catches exceptions, logs to app logger."""
        audit_log_path = tmp_path / "audit.log"
        init_audit_logger(audit_log_path)

        # Force an error by removing the file and making directory unwritable
        audit_log_path.unlink(missing_ok=True)
        tmp_path.chmod(0o444)

        try:
            with caplog.at_level(logging.ERROR):
                # Should NOT raise
                log_action("user@test.com", "login", "session", "s1")
        finally:
            # Restore permissions for cleanup
            tmp_path.chmod(0o755)

    def test_string_object_id_supported(self, audit_log_path):
        """object_id can be a string (e.g., session token)."""
        init_audit_logger(audit_log_path)
        log_action("user@test.com", "ip_changed", "session", "abc123")
        content = audit_log_path.read_text().strip()
        entry = json.loads(content)
        assert entry["object_id"] == "abc123"

    def test_handles_non_serializable_details(self, audit_log_path):
        """The default=str in json.dumps handles non-standard types."""
        init_audit_logger(audit_log_path)
        details = {"when": datetime(2024, 1, 1, tzinfo=timezone.utc)}
        log_action("admin@test.com", "login", "session", "s1", details)
        content = audit_log_path.read_text().strip()
        entry = json.loads(content)
        # datetime should be serialized as string via default=str
        assert "2024" in entry["details"]["when"]
