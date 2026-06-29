"""
Configuration dataclasses and JSON loader for the Flight Card Scanner.
"""

import json
import logging
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
    flier_match_model: str | None = None
    auto_accept_threshold: float = 0.95

    @property
    def image_store_path(self) -> Path:
        """Images directory within the event data path."""
        return self.event_data_path / "images"

    @property
    def db_path(self) -> Path:
        """Database file within the event data path."""
        return self.event_data_path / "flight_cards.db"


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


def load_config(path: Path) -> AppConfig:
    """Load, parse, and validate application configuration from a JSON file.

    For any key that has a defined default and is absent from the file, the
    default is applied and a log message is emitted at INFO level identifying
    the key and the value used.

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
    event_data_path = Path(get_with_default("event_data_path", "./data"))

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
        ssl_certfile = Path(data["ssl_certfile"])
    if "ssl_keyfile" in data:
        ssl_keyfile = Path(data["ssl_keyfile"])

    # --- known_fliers_path / flier_match_model (optional) ---
    known_fliers_path: Path | None = None
    flier_match_model: str | None = None
    if "known_fliers_path" in data:
        known_fliers_path = Path(data["known_fliers_path"])
    if "flier_match_model" in data:
        flier_match_model = data["flier_match_model"]

    if known_fliers_path is not None and not known_fliers_path.exists():
        raise ConfigError(
            f"Known fliers file not found: {known_fliers_path}"
        )
    if known_fliers_path is None and flier_match_model is None:
        logger.info("Flier verification is disabled (no 'known_fliers_path' configured).")

    # --- auto_accept_threshold (optional, default 0.95) ---
    auto_accept_threshold = get_with_default("auto_accept_threshold", 0.95)

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
        flier_match_model=flier_match_model,
        auto_accept_threshold=auto_accept_threshold,
    )
