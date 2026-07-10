"""
Configuration dataclasses and JSON loader for the Flight Card Scanner.

This module provides:

- ``AppConfig`` / ``load_config``   -- original combined (legacy) config format.
- ``ServerConfig`` / ``load_app_config`` -- server-level config for multi-event deployments.
- ``EventConfig`` / ``load_event_config`` -- per-event config loaded from event directories.
"""

import json
import logging
import warnings
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .exceptions import ConfigError

logger = logging.getLogger(__name__)

# Valid extraction mode values
_VALID_EXTRACTION_MODES = {"immediate", "deferred"}


@dataclass
class EndpointConfig:
    """Configuration for a single Ollama extraction endpoint."""
    url: str
    concurrency: int = 1


@dataclass
class DateRange:
    """Inclusive date range for the launch event."""
    start: date
    end: date


@dataclass
class AppConfig:
    """Top-level application configuration."""
    host: str = "0.0.0.0"
    port: int = 8000
    event_data_path: Path = field(default_factory=lambda: Path("./data"))
    event_name: str = "Flight Card Scanner"
    event_date_range: DateRange = field(
        default_factory=lambda: DateRange(start=date.today(), end=date.today())
    )
    extraction_mode: str = "immediate"  # "immediate" | "deferred"
    extraction_endpoints: list[EndpointConfig] = field(
        default_factory=lambda: [EndpointConfig(url="http://localhost:11434", concurrency=1)]
    )
    ssl_certfile: Path | None = None
    ssl_keyfile: Path | None = None
    known_fliers_path: Path | None = None
    auto_accept_threshold: float = 0.95
    read_only: bool = False
    auth_db_path: Path = field(default_factory=lambda: Path("./auth.db"))
    session_timeout_hours: float = 8.0
    audit_log_path: Path | None = None  # defaults to {event_data_path}/audit.log

    @property
    def image_store_path(self) -> Path:
        """Images directory within the event data path."""
        return self.event_data_path / "images"

    @property
    def db_path(self) -> Path:
        """Database file within the event data path."""
        return self.event_data_path / "flight_cards.db"

    @property
    def effective_audit_log_path(self) -> Path:
        """Audit log path, defaulting to {event_data_path}/audit.log."""
        if self.audit_log_path:
            return self.audit_log_path
        return self.event_data_path / "audit.log"


def _parse_date(value: str, field_name: str) -> date:
    """Parse an ISO 8601 date string, raising ConfigError on failure."""
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ConfigError(
            f"Invalid date for '{field_name}': {value!r}. Expected ISO 8601 format YYYY-MM-DD."
        ) from exc


def _parse_endpoint(obj: Any, index: int) -> EndpointConfig:
    """Parse a single extraction endpoint dict, raising ConfigError on invalid values."""
    if not isinstance(obj, dict):
        raise ConfigError(
            f"extraction_endpoints[{index}] must be an object, got {type(obj).__name__!r}."
        )
    url = obj.get("url")
    if not url or not isinstance(url, str):
        raise ConfigError(
            f"extraction_endpoints[{index}].url must be a non-empty string."
        )
    concurrency = obj.get("concurrency", 1)
    if not isinstance(concurrency, int) or concurrency < 1:
        raise ConfigError(
            f"extraction_endpoints[{index}].concurrency must be a positive integer ≥ 1, "
            f"got {concurrency!r}."
        )
    return EndpointConfig(url=url, concurrency=concurrency)


def _resolve_path(p: Path, config_dir: Path) -> Path:
    """Resolve a path relative to the config file's directory.

    Absolute paths are returned unchanged.  Relative paths are resolved
    against *config_dir* (the directory containing the JSON config file).
    """
    if p.is_absolute():
        return p
    return (config_dir / p).resolve()


def load_config(path: Path) -> AppConfig:
    """Load, parse, and validate application configuration from a JSON file.

    For any key that has a defined default and is absent from the file, the
    default is applied and a log message is emitted at INFO level identifying
    the key and the value used.

    Path values that are not absolute are resolved relative to the directory
    containing the configuration file.  This allows the same config to work
    regardless of the process working directory.

    Raises:
        ConfigError: If the file is missing, is not valid JSON, or contains
                     invalid values (e.g., unknown extraction_mode, concurrency
                     < 1, invalid date strings).
    """
    # --- Read file ---
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Configuration file not found: {path}"
        ) from exc
    except OSError as exc:
        raise ConfigError(
            f"Cannot read configuration file {path}: {exc}"
        ) from exc

    # Base directory for resolving relative paths in the config
    config_dir = Path(path).resolve().parent

    # --- Parse JSON ---
    try:
        data: dict = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Configuration file {path} is not valid JSON: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise ConfigError(
            f"Configuration file {path} must contain a JSON object at the top level."
        )

    # Helper: fetch a key with a default, logging if the default is used
    def get_with_default(key: str, default: Any) -> Any:
        if key not in data:
            logger.info(
                "Config key %r not found in %s; using default: %r",
                key, path, default,
            )
            return default
        return data[key]

    # --- host ---
    host = get_with_default("host", "0.0.0.0")

    # --- port ---
    port = get_with_default("port", 8000)
    if not isinstance(port, int) or isinstance(port, bool):
        raise ConfigError(f"Config key 'port' must be an integer, got {port!r}.")

    # --- event_data_path ---
    event_data_path = _resolve_path(
        Path(get_with_default("event_data_path", "./data")), config_dir
    )

    # --- event_name ---
    event_name = get_with_default("event_name", "Flight Card Scanner")

    # --- event_date_range ---
    if "event_date_range" not in data:
        today = date.today()
        logger.info(
            "Config key 'event_date_range' not found in %s; using default: start=%s end=%s",
            path, today.isoformat(), today.isoformat(),
        )
        event_date_range = DateRange(start=today, end=today)
    else:
        dr = data["event_date_range"]
        if not isinstance(dr, dict):
            raise ConfigError(
                f"Config key 'event_date_range' must be an object, got {type(dr).__name__!r}."
            )
        if "start" not in dr:
            raise ConfigError("Config key 'event_date_range.start' is required.")
        if "end" not in dr:
            raise ConfigError("Config key 'event_date_range.end' is required.")
        start_date = _parse_date(dr["start"], "event_date_range.start")
        end_date = _parse_date(dr["end"], "event_date_range.end")
        if end_date < start_date:
            raise ConfigError(
                f"event_date_range.end ({end_date}) must not be before "
                f"event_date_range.start ({start_date})."
            )
        event_date_range = DateRange(start=start_date, end=end_date)

    # --- extraction_mode ---
    extraction_mode = get_with_default("extraction_mode", "immediate")
    if extraction_mode not in _VALID_EXTRACTION_MODES:
        raise ConfigError(
            f"Config key 'extraction_mode' must be one of {sorted(_VALID_EXTRACTION_MODES)}, "
            f"got {extraction_mode!r}."
        )

    # --- extraction_endpoints ---
    if "extraction_endpoints" not in data:
        default_ep = [EndpointConfig(url="http://localhost:11434", concurrency=1)]
        logger.info(
            "Config key 'extraction_endpoints' not found in %s; using default: %r",
            path, default_ep,
        )
        extraction_endpoints = default_ep
    else:
        raw_endpoints = data["extraction_endpoints"]
        if not isinstance(raw_endpoints, list) or len(raw_endpoints) == 0:
            raise ConfigError(
                "Config key 'extraction_endpoints' must be a non-empty array."
            )
        extraction_endpoints = [
            _parse_endpoint(ep, i) for i, ep in enumerate(raw_endpoints)
        ]

    # --- ssl_certfile / ssl_keyfile (optional) ---
    ssl_certfile = None
    ssl_keyfile = None
    if "ssl_certfile" in data:
        ssl_certfile = _resolve_path(Path(data["ssl_certfile"]), config_dir)
    if "ssl_keyfile" in data:
        ssl_keyfile = _resolve_path(Path(data["ssl_keyfile"]), config_dir)

    # --- known_fliers_path (optional) ---
    known_fliers_path: Path | None = None
    if "known_fliers_path" in data:
        known_fliers_path = _resolve_path(Path(data["known_fliers_path"]), config_dir)

    if known_fliers_path is not None and not known_fliers_path.exists():
        raise ConfigError(
            f"Known fliers file not found: {known_fliers_path}"
        )
    if known_fliers_path is None:
        logger.info("Flier verification is disabled (no 'known_fliers_path' configured).")

    # --- auto_accept_threshold (optional, default 0.95) ---
    auto_accept_threshold = get_with_default("auto_accept_threshold", 0.95)

    # --- read_only (optional, default false) ---
    read_only = get_with_default("read_only", False)
    if not isinstance(read_only, bool):
        raise ConfigError(f"Config key 'read_only' must be a boolean, got {read_only!r}.")

    # --- auth_db_path (optional, default ./auth.db resolved relative to config dir) ---
    auth_db_path = _resolve_path(
        Path(get_with_default("auth_db_path", "./auth.db")), config_dir
    )

    # --- session_timeout_hours (optional, default 8, range [0.25, 8]) ---
    session_timeout_hours = get_with_default("session_timeout_hours", 8)
    if not isinstance(session_timeout_hours, (int, float)) or isinstance(session_timeout_hours, bool):
        raise ConfigError(
            f"Config key 'session_timeout_hours' must be a number, got {session_timeout_hours!r}."
        )
    session_timeout_hours = float(session_timeout_hours)
    if session_timeout_hours < 0.25 or session_timeout_hours > 8:
        raise ConfigError(
            f"Config key 'session_timeout_hours' must be between 0.25 and 8 inclusive, "
            f"got {session_timeout_hours}."
        )

    # --- audit_log_path (optional, defaults to {event_data_path}/audit.log) ---
    audit_log_path: Path | None = None
    if "audit_log_path" in data and data["audit_log_path"] is not None:
        audit_log_path = _resolve_path(Path(data["audit_log_path"]), config_dir)

    return AppConfig(
        host=host,
        port=port,
        event_data_path=event_data_path,
        event_name=event_name,
        event_date_range=event_date_range,
        extraction_mode=extraction_mode,
        extraction_endpoints=extraction_endpoints,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
        known_fliers_path=known_fliers_path,
        auto_accept_threshold=auto_accept_threshold,
        read_only=read_only,
        auth_db_path=auth_db_path,
        session_timeout_hours=session_timeout_hours,
        audit_log_path=audit_log_path,
    )


# ---------------------------------------------------------------------------
# Multi-event configuration: ServerConfig + EventConfig
# ---------------------------------------------------------------------------


@dataclass
class ServerConfig:
    """Server-level (app-wide) configuration for multi-event deployments.

    This is the new config format where server settings are separated from
    per-event settings.  The ``events_dir`` field points to the directory tree
    that is scanned for per-event ``config.json`` files.
    """

    host: str = "0.0.0.0"
    port: int = 8000
    events_dir: Path = field(default_factory=lambda: Path("./events"))
    thrustcurve_cache_path: Path = field(
        default_factory=lambda: Path("./thrustcurve_cache")
    )
    ssl_certfile: Path | None = None
    ssl_keyfile: Path | None = None
    auth_db_path: Path = field(default_factory=lambda: Path("./auth.db"))
    session_timeout_hours: float = 8.0
    event_idle_timeout_minutes: int = 60


@dataclass
class EventConfig:
    """Per-event configuration loaded from a config.json inside the events tree.

    The ``event_data_path`` is set to the directory containing the event's
    config.json file.  All relative paths in the event config are resolved
    against that directory.
    """

    event_name: str = "Flight Card Scanner"
    event_data_path: Path = field(default_factory=lambda: Path("."))
    event_date_range: DateRange = field(
        default_factory=lambda: DateRange(start=date.today(), end=date.today())
    )
    extraction_mode: str = "immediate"
    extraction_endpoints: list[EndpointConfig] = field(
        default_factory=lambda: [
            EndpointConfig(url="http://localhost:11434", concurrency=1)
        ]
    )
    known_fliers_path: Path | None = None
    auto_accept_threshold: float = 0.95
    read_only: bool = False
    audit_log_path: Path | None = None

    @property
    def image_store_path(self) -> Path:
        """Images directory within the event data path."""
        return self.event_data_path / "images"

    @property
    def db_path(self) -> Path:
        """Database file within the event data path."""
        return self.event_data_path / "flight_cards.db"

    @property
    def effective_audit_log_path(self) -> Path:
        """Audit log path, defaulting to {event_data_path}/audit.log."""
        if self.audit_log_path:
            return self.audit_log_path
        return self.event_data_path / "audit.log"


def load_app_config(path: Path) -> ServerConfig:
    """Load server-level configuration from a JSON file.

    This reads the new app-level config format used in multi-event
    deployments.  The config file should contain server settings only
    (host, port, events_dir, etc.) without per-event fields.

    If the config file contains both server fields (``host``) and event
    fields (``event_name``), a deprecation warning is emitted indicating
    that the combined format is deprecated.

    Paths are resolved relative to the directory containing the config file.

    Raises:
        ConfigError: If the file is missing, not valid JSON, or contains
                     invalid values.
    """
    # --- Read file ---
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Configuration file not found: {path}"
        ) from exc
    except OSError as exc:
        raise ConfigError(
            f"Cannot read configuration file {path}: {exc}"
        ) from exc

    config_dir = Path(path).resolve().parent

    # --- Parse JSON ---
    try:
        data: dict = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Configuration file {path} is not valid JSON: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise ConfigError(
            f"Configuration file {path} must contain a JSON object at the top level."
        )

    # Detect combined (legacy) config format
    _has_server_fields = "host" in data or "port" in data
    _has_event_fields = "event_name" in data or "event_date_range" in data
    if _has_server_fields and _has_event_fields:
        warnings.warn(
            "Combined app+event config format is deprecated. "
            "Separate server config from per-event configs in the events directory.",
            DeprecationWarning,
            stacklevel=2,
        )
        logger.warning(
            "Config file %s uses the deprecated combined format. "
            "Separate server config from per-event configs.",
            path,
        )

    # Helper: fetch with default and log
    def get_with_default(key: str, default: Any) -> Any:
        if key not in data:
            logger.info(
                "Config key %r not found in %s; using default: %r",
                key, path, default,
            )
            return default
        return data[key]

    # --- host ---
    host = get_with_default("host", "0.0.0.0")

    # --- port ---
    port = get_with_default("port", 8000)
    if not isinstance(port, int) or isinstance(port, bool):
        raise ConfigError(f"Config key 'port' must be an integer, got {port!r}.")

    # --- events_dir ---
    events_dir = _resolve_path(
        Path(get_with_default("events_dir", "./events")), config_dir
    )

    # --- thrustcurve_cache_path ---
    thrustcurve_cache_path = _resolve_path(
        Path(get_with_default("thrustcurve_cache_path", "./thrustcurve_cache")),
        config_dir,
    )

    # --- ssl_certfile / ssl_keyfile (optional) ---
    ssl_certfile: Path | None = None
    ssl_keyfile: Path | None = None
    if "ssl_certfile" in data:
        ssl_certfile = _resolve_path(Path(data["ssl_certfile"]), config_dir)
    if "ssl_keyfile" in data:
        ssl_keyfile = _resolve_path(Path(data["ssl_keyfile"]), config_dir)

    # --- auth_db_path ---
    auth_db_path = _resolve_path(
        Path(get_with_default("auth_db_path", "./auth.db")), config_dir
    )

    # --- session_timeout_hours ---
    session_timeout_hours = get_with_default("session_timeout_hours", 8)
    if not isinstance(session_timeout_hours, (int, float)) or isinstance(
        session_timeout_hours, bool
    ):
        raise ConfigError(
            f"Config key 'session_timeout_hours' must be a number, got {session_timeout_hours!r}."
        )
    session_timeout_hours = float(session_timeout_hours)
    if session_timeout_hours < 0.25 or session_timeout_hours > 8:
        raise ConfigError(
            f"Config key 'session_timeout_hours' must be between 0.25 and 8 inclusive, "
            f"got {session_timeout_hours}."
        )

    # --- event_idle_timeout_minutes ---
    event_idle_timeout_minutes = get_with_default("event_idle_timeout_minutes", 60)
    if (
        not isinstance(event_idle_timeout_minutes, int)
        or isinstance(event_idle_timeout_minutes, bool)
    ):
        raise ConfigError(
            f"Config key 'event_idle_timeout_minutes' must be an integer, "
            f"got {event_idle_timeout_minutes!r}."
        )
    if event_idle_timeout_minutes < 1:
        raise ConfigError(
            f"Config key 'event_idle_timeout_minutes' must be >= 1, "
            f"got {event_idle_timeout_minutes}."
        )

    return ServerConfig(
        host=host,
        port=port,
        events_dir=events_dir,
        thrustcurve_cache_path=thrustcurve_cache_path,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
        auth_db_path=auth_db_path,
        session_timeout_hours=session_timeout_hours,
        event_idle_timeout_minutes=event_idle_timeout_minutes,
    )


def load_event_config(path: Path) -> EventConfig:
    """Load per-event configuration from a config.json in the events tree.

    The ``event_data_path`` is set to the directory containing this config
    file.  All relative paths are resolved against that directory.

    Raises:
        ConfigError: If the file is missing, not valid JSON, or contains
                     invalid values.
    """
    # --- Read file ---
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Event configuration file not found: {path}"
        ) from exc
    except OSError as exc:
        raise ConfigError(
            f"Cannot read event configuration file {path}: {exc}"
        ) from exc

    config_dir = Path(path).resolve().parent

    # --- Parse JSON ---
    try:
        data: dict = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Event configuration file {path} is not valid JSON: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise ConfigError(
            f"Event configuration file {path} must contain a JSON object at the top level."
        )

    # Helper: fetch with default and log
    def get_with_default(key: str, default: Any) -> Any:
        if key not in data:
            logger.info(
                "Event config key %r not found in %s; using default: %r",
                key, path, default,
            )
            return default
        return data[key]

    # --- event_data_path (the directory containing this config.json) ---
    event_data_path = config_dir

    # --- event_name ---
    event_name = get_with_default("event_name", "Flight Card Scanner")

    # --- event_date_range ---
    if "event_date_range" not in data:
        today = date.today()
        logger.info(
            "Event config key 'event_date_range' not found in %s; "
            "using default: start=%s end=%s",
            path,
            today.isoformat(),
            today.isoformat(),
        )
        event_date_range = DateRange(start=today, end=today)
    else:
        dr = data["event_date_range"]
        if not isinstance(dr, dict):
            raise ConfigError(
                f"Config key 'event_date_range' must be an object, got {type(dr).__name__!r}."
            )
        if "start" not in dr:
            raise ConfigError("Config key 'event_date_range.start' is required.")
        if "end" not in dr:
            raise ConfigError("Config key 'event_date_range.end' is required.")
        start_date = _parse_date(dr["start"], "event_date_range.start")
        end_date = _parse_date(dr["end"], "event_date_range.end")
        if end_date < start_date:
            raise ConfigError(
                f"event_date_range.end ({end_date}) must not be before "
                f"event_date_range.start ({start_date})."
            )
        event_date_range = DateRange(start=start_date, end=end_date)

    # --- extraction_mode ---
    extraction_mode = get_with_default("extraction_mode", "immediate")
    if extraction_mode not in _VALID_EXTRACTION_MODES:
        raise ConfigError(
            f"Config key 'extraction_mode' must be one of {sorted(_VALID_EXTRACTION_MODES)}, "
            f"got {extraction_mode!r}."
        )

    # --- extraction_endpoints ---
    if "extraction_endpoints" not in data:
        default_ep = [EndpointConfig(url="http://localhost:11434", concurrency=1)]
        logger.info(
            "Event config key 'extraction_endpoints' not found in %s; using default: %r",
            path,
            default_ep,
        )
        extraction_endpoints = default_ep
    else:
        raw_endpoints = data["extraction_endpoints"]
        if not isinstance(raw_endpoints, list) or len(raw_endpoints) == 0:
            raise ConfigError(
                "Config key 'extraction_endpoints' must be a non-empty array."
            )
        extraction_endpoints = [
            _parse_endpoint(ep, i) for i, ep in enumerate(raw_endpoints)
        ]

    # --- known_fliers_path (optional) ---
    known_fliers_path: Path | None = None
    if "known_fliers_path" in data:
        known_fliers_path = _resolve_path(Path(data["known_fliers_path"]), config_dir)

    if known_fliers_path is not None and not known_fliers_path.exists():
        raise ConfigError(
            f"Known fliers file not found: {known_fliers_path}"
        )
    if known_fliers_path is None:
        logger.info(
            "Flier verification is disabled (no 'known_fliers_path' in event config)."
        )

    # --- auto_accept_threshold ---
    auto_accept_threshold = get_with_default("auto_accept_threshold", 0.95)

    # --- read_only ---
    read_only = get_with_default("read_only", False)
    if not isinstance(read_only, bool):
        raise ConfigError(
            f"Config key 'read_only' must be a boolean, got {read_only!r}."
        )

    # --- audit_log_path (optional) ---
    audit_log_path: Path | None = None
    if "audit_log_path" in data and data["audit_log_path"] is not None:
        audit_log_path = _resolve_path(Path(data["audit_log_path"]), config_dir)

    return EventConfig(
        event_name=event_name,
        event_data_path=event_data_path,
        event_date_range=event_date_range,
        extraction_mode=extraction_mode,
        extraction_endpoints=extraction_endpoints,
        known_fliers_path=known_fliers_path,
        auto_accept_threshold=auto_accept_threshold,
        read_only=read_only,
        audit_log_path=audit_log_path,
    )
