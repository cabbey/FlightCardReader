"""
Tests for the multi-event config split: ServerConfig, EventConfig,
load_app_config(), and load_event_config().
"""

import json
import tempfile
import warnings
from datetime import date
from pathlib import Path

import pytest

from flight_card_scanner.config import (
    AppConfig,
    DateRange,
    EndpointConfig,
    EventConfig,
    ServerConfig,
    load_app_config,
    load_config,
    load_event_config,
)
from flight_card_scanner.exceptions import ConfigError


def _write_config(data: dict, directory: Path | None = None) -> Path:
    """Write a config dict to a temporary JSON file and return the path."""
    kwargs = {"mode": "w", "suffix": ".json", "delete": False, "encoding": "utf-8"}
    if directory is not None:
        kwargs["dir"] = str(directory)
    f = tempfile.NamedTemporaryFile(**kwargs)
    json.dump(data, f)
    f.close()
    return Path(f.name)


# ---------------------------------------------------------------------------
# ServerConfig / load_app_config tests
# ---------------------------------------------------------------------------


class TestServerConfigDataclass:
    """Tests for the ServerConfig dataclass defaults and fields."""

    def test_default_values(self):
        """ServerConfig has sensible defaults for all fields."""
        cfg = ServerConfig()
        assert cfg.host == "0.0.0.0"
        assert cfg.port == 8000
        assert cfg.events_dir == Path("./events")
        assert cfg.thrustcurve_cache_path == Path("./thrustcurve_cache")
        assert cfg.ssl_certfile is None
        assert cfg.ssl_keyfile is None
        assert cfg.auth_db_path == Path("./auth.db")
        assert cfg.session_timeout_hours == 8.0
        assert cfg.event_idle_timeout_minutes == 60


class TestLoadAppConfig:
    """Tests for load_app_config()."""

    def test_minimal_config(self, tmp_path):
        """A minimal config file produces a ServerConfig with defaults."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({}), encoding="utf-8")
        cfg = load_app_config(config_file)
        assert isinstance(cfg, ServerConfig)
        assert cfg.host == "0.0.0.0"
        assert cfg.port == 8000
        assert cfg.events_dir == (tmp_path / "events").resolve()
        assert cfg.thrustcurve_cache_path == (tmp_path / "thrustcurve_cache").resolve()
        assert cfg.auth_db_path == (tmp_path / "auth.db").resolve()
        assert cfg.session_timeout_hours == 8.0
        assert cfg.event_idle_timeout_minutes == 60
        assert cfg.ssl_certfile is None
        assert cfg.ssl_keyfile is None

    def test_full_config(self, tmp_path):
        """All fields are read from the config file correctly."""
        data = {
            "host": "127.0.0.1",
            "port": 9000,
            "events_dir": "/srv/events",
            "thrustcurve_cache_path": "/srv/tc_cache",
            "ssl_certfile": "/etc/ssl/cert.pem",
            "ssl_keyfile": "/etc/ssl/key.pem",
            "auth_db_path": "/srv/auth.db",
            "session_timeout_hours": 4.0,
            "event_idle_timeout_minutes": 30,
        }
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(data), encoding="utf-8")
        cfg = load_app_config(config_file)
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 9000
        assert cfg.events_dir == Path("/srv/events")
        assert cfg.thrustcurve_cache_path == Path("/srv/tc_cache")
        assert cfg.ssl_certfile == Path("/etc/ssl/cert.pem")
        assert cfg.ssl_keyfile == Path("/etc/ssl/key.pem")
        assert cfg.auth_db_path == Path("/srv/auth.db")
        assert cfg.session_timeout_hours == 4.0
        assert cfg.event_idle_timeout_minutes == 30

    def test_relative_paths_resolved_against_config_dir(self, tmp_path):
        """Relative paths are resolved against the config file directory."""
        data = {
            "events_dir": "./my_events",
            "thrustcurve_cache_path": "cache/tc",
            "auth_db_path": "db/auth.db",
        }
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(data), encoding="utf-8")
        cfg = load_app_config(config_file)
        assert cfg.events_dir == (tmp_path / "my_events").resolve()
        assert cfg.thrustcurve_cache_path == (tmp_path / "cache" / "tc").resolve()
        assert cfg.auth_db_path == (tmp_path / "db" / "auth.db").resolve()

    def test_null_ssl_fields_do_not_crash(self, tmp_path):
        """SSL fields set to JSON null should result in None, not a crash."""
        data = {
            "ssl_certfile": None,
            "ssl_keyfile": None,
        }
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(data), encoding="utf-8")
        cfg = load_app_config(config_file)
        assert cfg.ssl_certfile is None
        assert cfg.ssl_keyfile is None

    def test_missing_file_raises_config_error(self):
        """A missing config file raises ConfigError."""
        with pytest.raises(ConfigError, match="not found"):
            load_app_config(Path("/nonexistent/path/config.json"))

    def test_invalid_json_raises_config_error(self, tmp_path):
        """Invalid JSON raises ConfigError."""
        config_file = tmp_path / "config.json"
        config_file.write_text("not json", encoding="utf-8")
        with pytest.raises(ConfigError, match="not valid JSON"):
            load_app_config(config_file)

    def test_non_object_raises_config_error(self, tmp_path):
        """A JSON array at the top level raises ConfigError."""
        config_file = tmp_path / "config.json"
        config_file.write_text("[]", encoding="utf-8")
        with pytest.raises(ConfigError, match="JSON object"):
            load_app_config(config_file)

    def test_invalid_port_raises_config_error(self, tmp_path):
        """A non-integer port raises ConfigError."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"port": "abc"}), encoding="utf-8")
        with pytest.raises(ConfigError, match="port"):
            load_app_config(config_file)

    def test_invalid_session_timeout_raises_config_error(self, tmp_path):
        """An out-of-range session_timeout_hours raises ConfigError."""
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps({"session_timeout_hours": 100}), encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="0.25.*8"):
            load_app_config(config_file)

    def test_invalid_event_idle_timeout_type_raises_config_error(self, tmp_path):
        """A non-integer event_idle_timeout_minutes raises ConfigError."""
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps({"event_idle_timeout_minutes": "thirty"}), encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="event_idle_timeout_minutes"):
            load_app_config(config_file)

    def test_invalid_event_idle_timeout_zero_raises_config_error(self, tmp_path):
        """event_idle_timeout_minutes must be >= 1."""
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps({"event_idle_timeout_minutes": 0}), encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="event_idle_timeout_minutes"):
            load_app_config(config_file)

    def test_boolean_port_raises_config_error(self, tmp_path):
        """Boolean port (even though bool is int subclass) raises ConfigError."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"port": True}), encoding="utf-8")
        with pytest.raises(ConfigError, match="port"):
            load_app_config(config_file)

    def test_combined_format_emits_deprecation_warning(self, tmp_path):
        """A combined config (has host AND event_name) emits a DeprecationWarning."""
        data = {
            "host": "0.0.0.0",
            "port": 8000,
            "event_name": "Test Event",
        }
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(data), encoding="utf-8")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            cfg = load_app_config(config_file)
            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert "deprecated" in str(w[0].message).lower()
        assert isinstance(cfg, ServerConfig)


# ---------------------------------------------------------------------------
# EventConfig / load_event_config tests
# ---------------------------------------------------------------------------


class TestEventConfigDataclass:
    """Tests for the EventConfig dataclass defaults and computed properties."""

    def test_default_values(self):
        """EventConfig has sensible defaults."""
        cfg = EventConfig()
        assert cfg.event_name == "Flight Card Scanner"
        assert cfg.event_data_path == Path(".")
        assert cfg.extraction_mode == "immediate"
        assert cfg.auto_accept_threshold == 0.95
        assert cfg.read_only is False
        assert cfg.audit_log_path is None
        assert cfg.known_fliers_path is None

    def test_image_store_path_property(self):
        """image_store_path is event_data_path / 'images'."""
        cfg = EventConfig(event_data_path=Path("/events/2026/nxrs"))
        assert cfg.image_store_path == Path("/events/2026/nxrs/images")

    def test_db_path_property(self):
        """db_path is event_data_path / 'flight_cards.db'."""
        cfg = EventConfig(event_data_path=Path("/events/2026/nxrs"))
        assert cfg.db_path == Path("/events/2026/nxrs/flight_cards.db")

    def test_effective_audit_log_path_default(self):
        """When audit_log_path is None, defaults to event_data_path/audit.log."""
        cfg = EventConfig(event_data_path=Path("/events/2026/nxrs"))
        assert cfg.effective_audit_log_path == Path("/events/2026/nxrs/audit.log")

    def test_effective_audit_log_path_explicit(self):
        """When audit_log_path is set, uses that value."""
        cfg = EventConfig(
            event_data_path=Path("/events/2026/nxrs"),
            audit_log_path=Path("/var/log/audit.log"),
        )
        assert cfg.effective_audit_log_path == Path("/var/log/audit.log")


class TestLoadEventConfig:
    """Tests for load_event_config()."""

    def test_minimal_event_config(self, tmp_path):
        """A minimal event config results in defaults, with event_data_path = config dir."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({}), encoding="utf-8")
        cfg = load_event_config(config_file)
        assert isinstance(cfg, EventConfig)
        assert cfg.event_data_path == tmp_path.resolve()
        assert cfg.event_name == "Flight Card Scanner"
        assert cfg.extraction_mode == "immediate"
        assert cfg.auto_accept_threshold == 0.95
        assert cfg.read_only is False

    def test_full_event_config(self, tmp_path):
        """All event fields are read correctly."""
        data = {
            "event_name": "NXRS 2026",
            "event_date_range": {"start": "2026-06-01", "end": "2026-06-03"},
            "extraction_mode": "deferred",
            "extraction_endpoints": [
                {"url": "http://gpu1:11434", "concurrency": 2}
            ],
            "auto_accept_threshold": 0.90,
            "read_only": True,
            "audit_log_path": "/var/log/nxrs_audit.log",
        }
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(data), encoding="utf-8")
        cfg = load_event_config(config_file)
        assert cfg.event_name == "NXRS 2026"
        assert cfg.event_date_range == DateRange(
            start=date(2026, 6, 1), end=date(2026, 6, 3)
        )
        assert cfg.extraction_mode == "deferred"
        assert len(cfg.extraction_endpoints) == 1
        assert cfg.extraction_endpoints[0].url == "http://gpu1:11434"
        assert cfg.extraction_endpoints[0].concurrency == 2
        assert cfg.auto_accept_threshold == 0.90
        assert cfg.read_only is True
        assert cfg.audit_log_path == Path("/var/log/nxrs_audit.log")

    def test_event_data_path_is_config_directory(self, tmp_path):
        """event_data_path is always the directory containing the config file."""
        subdir = tmp_path / "2026" / "nxrs"
        subdir.mkdir(parents=True)
        config_file = subdir / "config.json"
        config_file.write_text(json.dumps({"event_name": "NXRS"}), encoding="utf-8")
        cfg = load_event_config(config_file)
        assert cfg.event_data_path == subdir.resolve()

    def test_known_fliers_path_existing_file(self, tmp_path):
        """known_fliers_path is resolved when file exists."""
        fliers_file = tmp_path / "fliers.tsv"
        fliers_file.write_text("Name\tNAR\n", encoding="utf-8")
        data = {"known_fliers_path": str(fliers_file)}
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(data), encoding="utf-8")
        cfg = load_event_config(config_file)
        assert cfg.known_fliers_path == fliers_file

    def test_known_fliers_path_missing_file_raises(self, tmp_path):
        """known_fliers_path pointing to a non-existent file raises ConfigError."""
        data = {"known_fliers_path": "/nonexistent/fliers.tsv"}
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ConfigError, match="Known fliers file not found"):
            load_event_config(config_file)

    def test_relative_known_fliers_path_resolved(self, tmp_path):
        """Relative known_fliers_path is resolved against config dir."""
        fliers_file = tmp_path / "data" / "fliers.tsv"
        fliers_file.parent.mkdir(parents=True)
        fliers_file.write_text("Name\tNAR\n", encoding="utf-8")
        data = {"known_fliers_path": "data/fliers.tsv"}
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(data), encoding="utf-8")
        cfg = load_event_config(config_file)
        assert cfg.known_fliers_path == fliers_file.resolve()

    def test_invalid_extraction_mode_raises(self, tmp_path):
        """An invalid extraction_mode raises ConfigError."""
        data = {"extraction_mode": "invalid_mode"}
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ConfigError, match="extraction_mode"):
            load_event_config(config_file)

    def test_invalid_date_range_raises(self, tmp_path):
        """An invalid date range (end < start) raises ConfigError."""
        data = {
            "event_date_range": {"start": "2026-06-03", "end": "2026-06-01"}
        }
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ConfigError, match="must not be before"):
            load_event_config(config_file)

    def test_missing_file_raises_config_error(self):
        """A non-existent config path raises ConfigError."""
        with pytest.raises(ConfigError, match="not found"):
            load_event_config(Path("/nonexistent/config.json"))

    def test_invalid_json_raises_config_error(self, tmp_path):
        """Invalid JSON raises ConfigError."""
        config_file = tmp_path / "config.json"
        config_file.write_text("{broken", encoding="utf-8")
        with pytest.raises(ConfigError, match="not valid JSON"):
            load_event_config(config_file)

    def test_empty_endpoints_list_raises(self, tmp_path):
        """An empty extraction_endpoints list raises ConfigError."""
        data = {"extraction_endpoints": []}
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ConfigError, match="non-empty array"):
            load_event_config(config_file)

    def test_read_only_non_bool_raises(self, tmp_path):
        """A non-boolean read_only raises ConfigError."""
        data = {"read_only": "yes"}
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ConfigError, match="read_only"):
            load_event_config(config_file)

    def test_audit_log_path_relative_resolved(self, tmp_path):
        """Relative audit_log_path is resolved against config dir."""
        data = {"audit_log_path": "logs/audit.log"}
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(data), encoding="utf-8")
        cfg = load_event_config(config_file)
        assert cfg.audit_log_path == (tmp_path / "logs" / "audit.log").resolve()


# ---------------------------------------------------------------------------
# Backward compatibility tests
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """The old load_config + AppConfig API still works exactly as before."""

    def test_old_appconfig_unchanged(self, tmp_path):
        """load_config() still returns AppConfig with all combined fields."""
        data = {
            "host": "127.0.0.1",
            "port": 9000,
            "event_data_path": str(tmp_path / "data"),
            "event_name": "Legacy Event",
            "event_date_range": {"start": "2026-01-01", "end": "2026-01-02"},
            "extraction_mode": "deferred",
            "extraction_endpoints": [
                {"url": "http://localhost:11434", "concurrency": 1}
            ],
            "session_timeout_hours": 4.0,
        }
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(data), encoding="utf-8")
        cfg = load_config(config_file)
        assert isinstance(cfg, AppConfig)
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 9000
        assert cfg.event_name == "Legacy Event"
        assert cfg.extraction_mode == "deferred"
        assert cfg.session_timeout_hours == 4.0

    def test_old_appconfig_still_has_all_fields(self):
        """AppConfig retains all fields from before the split."""
        cfg = AppConfig()
        # Server-level fields
        assert hasattr(cfg, "host")
        assert hasattr(cfg, "port")
        assert hasattr(cfg, "ssl_certfile")
        assert hasattr(cfg, "ssl_keyfile")
        assert hasattr(cfg, "auth_db_path")
        assert hasattr(cfg, "session_timeout_hours")
        # Event-level fields
        assert hasattr(cfg, "event_name")
        assert hasattr(cfg, "event_data_path")
        assert hasattr(cfg, "event_date_range")
        assert hasattr(cfg, "extraction_mode")
        assert hasattr(cfg, "extraction_endpoints")
        assert hasattr(cfg, "known_fliers_path")
        assert hasattr(cfg, "auto_accept_threshold")
        assert hasattr(cfg, "read_only")
        assert hasattr(cfg, "audit_log_path")
        # Properties
        assert hasattr(cfg, "image_store_path")
        assert hasattr(cfg, "db_path")
        assert hasattr(cfg, "effective_audit_log_path")
