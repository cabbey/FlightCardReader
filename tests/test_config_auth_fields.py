"""
Tests for auth-related config fields added in task 1.1.

Validates:
- auth_db_path defaults to ./auth.db resolved relative to config dir
- session_timeout_hours defaults to 8, validates range [0.25, 8]
- audit_log_path defaults to None (effective_audit_log_path uses event_data_path/audit.log)
- effective_audit_log_path property works correctly
"""

import json
import tempfile
from pathlib import Path

import pytest

from flight_card_scanner.config import load_config, AppConfig
from flight_card_scanner.exceptions import ConfigError


def _write_config(data: dict) -> Path:
    """Write a config dict to a temporary JSON file and return the path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(data, f)
    f.close()
    return Path(f.name)


# Minimal valid config that satisfies existing requirements
MINIMAL_CONFIG = {
    "extraction_endpoints": [{"url": "http://localhost:11434", "concurrency": 1}],
}


class TestAuthDbPath:
    """Tests for auth_db_path config field."""

    def test_default_auth_db_path(self):
        """When auth_db_path is absent, defaults to ./auth.db resolved relative to config dir."""
        config_file = _write_config(MINIMAL_CONFIG)
        cfg = load_config(config_file)
        config_dir = config_file.resolve().parent
        expected = (config_dir / "auth.db").resolve()
        assert cfg.auth_db_path == expected

    def test_explicit_absolute_auth_db_path(self):
        """When auth_db_path is an absolute path, it's used as-is."""
        data = {**MINIMAL_CONFIG, "auth_db_path": "/opt/auth/my.db"}
        config_file = _write_config(data)
        cfg = load_config(config_file)
        assert cfg.auth_db_path == Path("/opt/auth/my.db")

    def test_relative_auth_db_path_resolved(self):
        """When auth_db_path is relative, it's resolved against config file directory."""
        data = {**MINIMAL_CONFIG, "auth_db_path": "./subdir/auth.db"}
        config_file = _write_config(data)
        cfg = load_config(config_file)
        config_dir = config_file.resolve().parent
        expected = (config_dir / "subdir" / "auth.db").resolve()
        assert cfg.auth_db_path == expected


class TestSessionTimeoutHours:
    """Tests for session_timeout_hours config field."""

    def test_default_session_timeout(self):
        """When session_timeout_hours is absent, defaults to 8.0."""
        config_file = _write_config(MINIMAL_CONFIG)
        cfg = load_config(config_file)
        assert cfg.session_timeout_hours == 8.0

    def test_explicit_session_timeout(self):
        """When session_timeout_hours is set to a valid value, it's used."""
        data = {**MINIMAL_CONFIG, "session_timeout_hours": 4}
        config_file = _write_config(data)
        cfg = load_config(config_file)
        assert cfg.session_timeout_hours == 4.0

    def test_float_session_timeout(self):
        """Float values are accepted for session_timeout_hours."""
        data = {**MINIMAL_CONFIG, "session_timeout_hours": 2.5}
        config_file = _write_config(data)
        cfg = load_config(config_file)
        assert cfg.session_timeout_hours == 2.5

    def test_boundary_min_value(self):
        """0.25 is the minimum valid value."""
        data = {**MINIMAL_CONFIG, "session_timeout_hours": 0.25}
        config_file = _write_config(data)
        cfg = load_config(config_file)
        assert cfg.session_timeout_hours == 0.25

    def test_boundary_max_value(self):
        """8 is the maximum valid value."""
        data = {**MINIMAL_CONFIG, "session_timeout_hours": 8}
        config_file = _write_config(data)
        cfg = load_config(config_file)
        assert cfg.session_timeout_hours == 8.0

    def test_below_min_raises_config_error(self):
        """Values below 0.25 raise ConfigError."""
        data = {**MINIMAL_CONFIG, "session_timeout_hours": 0.1}
        config_file = _write_config(data)
        with pytest.raises(ConfigError, match="0.25.*8"):
            load_config(config_file)

    def test_above_max_raises_config_error(self):
        """Values above 8 raise ConfigError."""
        data = {**MINIMAL_CONFIG, "session_timeout_hours": 9}
        config_file = _write_config(data)
        with pytest.raises(ConfigError, match="0.25.*8"):
            load_config(config_file)

    def test_non_numeric_raises_config_error(self):
        """Non-numeric values raise ConfigError."""
        data = {**MINIMAL_CONFIG, "session_timeout_hours": "four"}
        config_file = _write_config(data)
        with pytest.raises(ConfigError, match="must be a number"):
            load_config(config_file)

    def test_boolean_raises_config_error(self):
        """Boolean values are rejected (even though bool is subclass of int in Python)."""
        data = {**MINIMAL_CONFIG, "session_timeout_hours": True}
        config_file = _write_config(data)
        with pytest.raises(ConfigError, match="must be a number"):
            load_config(config_file)

    def test_zero_raises_config_error(self):
        """Zero is below the minimum."""
        data = {**MINIMAL_CONFIG, "session_timeout_hours": 0}
        config_file = _write_config(data)
        with pytest.raises(ConfigError, match="0.25.*8"):
            load_config(config_file)

    def test_negative_raises_config_error(self):
        """Negative values are below the minimum."""
        data = {**MINIMAL_CONFIG, "session_timeout_hours": -1}
        config_file = _write_config(data)
        with pytest.raises(ConfigError, match="0.25.*8"):
            load_config(config_file)


class TestAuditLogPath:
    """Tests for audit_log_path and effective_audit_log_path."""

    def test_default_audit_log_path_is_none(self):
        """When audit_log_path is absent, it defaults to None."""
        config_file = _write_config(MINIMAL_CONFIG)
        cfg = load_config(config_file)
        assert cfg.audit_log_path is None

    def test_effective_audit_log_path_default(self):
        """When audit_log_path is None, effective_audit_log_path uses event_data_path/audit.log."""
        config_file = _write_config(MINIMAL_CONFIG)
        cfg = load_config(config_file)
        assert cfg.effective_audit_log_path == cfg.event_data_path / "audit.log"

    def test_explicit_audit_log_path(self):
        """When audit_log_path is set, it's used directly."""
        data = {**MINIMAL_CONFIG, "audit_log_path": "/var/log/fcs_audit.log"}
        config_file = _write_config(data)
        cfg = load_config(config_file)
        assert cfg.audit_log_path == Path("/var/log/fcs_audit.log")
        assert cfg.effective_audit_log_path == Path("/var/log/fcs_audit.log")

    def test_null_audit_log_path_treated_as_absent(self):
        """When audit_log_path is explicitly null in JSON, it's treated as None."""
        data = {**MINIMAL_CONFIG, "audit_log_path": None}
        config_file = _write_config(data)
        cfg = load_config(config_file)
        assert cfg.audit_log_path is None
        assert cfg.effective_audit_log_path == cfg.event_data_path / "audit.log"

    def test_relative_audit_log_path_resolved(self):
        """When audit_log_path is relative, it's resolved against config file directory."""
        data = {**MINIMAL_CONFIG, "audit_log_path": "./logs/audit.log"}
        config_file = _write_config(data)
        cfg = load_config(config_file)
        config_dir = config_file.resolve().parent
        expected = (config_dir / "logs" / "audit.log").resolve()
        assert cfg.audit_log_path == expected
        assert cfg.effective_audit_log_path == expected
