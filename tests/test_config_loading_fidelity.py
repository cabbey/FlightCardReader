# Feature: flight-card-scanner, Property 19: Config loading fidelity
"""
Property-based test for config loading fidelity.

Generate arbitrary valid config dicts with all keys present and assert
`load_config` returns matching `AppConfig` fields; also generate configs
with optional keys absent and assert documented defaults are applied.

**Validates: Requirements 9.2, 9.3**
"""
import json
import tempfile
from datetime import date
from pathlib import Path

import pytest
from hypothesis import given, assume, settings
from hypothesis import strategies as st

from flight_card_scanner.config import load_config, AppConfig, EndpointConfig, DateRange


# --- Strategies ---

# Valid host strings
hosts = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters=".-_"),
    min_size=1,
    max_size=50,
)

# Valid port integers
ports = st.integers(min_value=1, max_value=65535)

# Valid filesystem path strings (relative or absolute, no empty)
path_strings = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="/._ -"),
    min_size=1,
    max_size=100,
).filter(lambda s: s.strip() != "")

# Valid event name strings (non-empty)
event_names = st.text(min_size=1, max_size=200)

# Valid extraction modes
extraction_modes = st.sampled_from(["immediate", "deferred"])

# Valid date ranges (end >= start)
date_ranges = st.tuples(
    st.dates(min_value=date(2000, 1, 1), max_value=date(2099, 12, 31)),
    st.dates(min_value=date(2000, 1, 1), max_value=date(2099, 12, 31)),
).map(lambda t: (min(t[0], t[1]), max(t[0], t[1])))

# Valid endpoint URL (non-empty string that looks like a URL)
endpoint_urls = st.from_regex(r"https?://[a-z0-9][a-z0-9.\-]*(:[0-9]{1,5})?", fullmatch=True)

# Valid concurrency (positive int >= 1)
concurrencies = st.integers(min_value=1, max_value=100)

# A single valid endpoint config dict
endpoint_dicts = st.fixed_dictionaries({
    "url": endpoint_urls,
    "concurrency": concurrencies,
})

# A valid non-empty list of endpoint dicts
endpoint_lists = st.lists(endpoint_dicts, min_size=1, max_size=5)


# Full config dict with all keys present
@st.composite
def full_config_dicts(draw):
    start, end = draw(date_ranges)
    return {
        "host": draw(hosts),
        "port": draw(ports),
        "event_data_path": draw(path_strings),
        "event_name": draw(event_names),
        "event_date_range": {
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
        "extraction_mode": draw(extraction_modes),
        "extraction_endpoints": draw(endpoint_lists),
    }


# Config dict with a random subset of optional keys absent
OPTIONAL_KEYS = [
    "host", "port", "event_data_path",
    "event_name", "event_date_range", "extraction_mode",
    "extraction_endpoints",
]


@st.composite
def partial_config_dicts(draw):
    """Generate a config dict with at least one optional key missing."""
    # Decide which keys to include (at least one must be missing)
    included = draw(
        st.lists(
            st.sampled_from(OPTIONAL_KEYS),
            min_size=0,
            max_size=len(OPTIONAL_KEYS) - 1,
            unique=True,
        )
    )
    # Ensure at least one key is absent
    assume(len(included) < len(OPTIONAL_KEYS))

    config = {}
    if "host" in included:
        config["host"] = draw(hosts)
    if "port" in included:
        config["port"] = draw(ports)
    if "event_data_path" in included:
        config["event_data_path"] = draw(path_strings)
    if "event_name" in included:
        config["event_name"] = draw(event_names)
    if "event_date_range" in included:
        start, end = draw(date_ranges)
        config["event_date_range"] = {
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
    if "extraction_mode" in included:
        config["extraction_mode"] = draw(extraction_modes)
    if "extraction_endpoints" in included:
        config["extraction_endpoints"] = draw(endpoint_lists)

    return config


# --- Tests ---

class TestConfigLoadingFidelity:
    """Property 19: Config loading fidelity."""

    @given(config_dict=full_config_dicts())
    @settings(max_examples=50)
    def test_full_config_fields_match(self, config_dict: dict):
        """When all keys are present, load_config returns AppConfig with matching fields."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(config_dict, f)
            config_file = Path(f.name)

        result = load_config(config_file)

        assert isinstance(result, AppConfig)
        assert result.host == config_dict["host"]
        assert result.port == config_dict["port"]
        # load_config resolves relative paths against the config file's directory
        raw_path = Path(config_dict["event_data_path"])
        if raw_path.is_absolute():
            expected_data_path = raw_path
        else:
            expected_data_path = (config_file.resolve().parent / raw_path).resolve()
        assert result.event_data_path == expected_data_path
        assert result.image_store_path == result.event_data_path / "images"
        assert result.db_path == result.event_data_path / "flight_cards.db"
        assert result.event_name == config_dict["event_name"]
        assert result.extraction_mode == config_dict["extraction_mode"]

        # Check date range
        dr = config_dict["event_date_range"]
        assert result.event_date_range.start == date.fromisoformat(dr["start"])
        assert result.event_date_range.end == date.fromisoformat(dr["end"])

        # Check endpoints
        assert len(result.extraction_endpoints) == len(config_dict["extraction_endpoints"])
        for actual_ep, expected_ep in zip(
            result.extraction_endpoints, config_dict["extraction_endpoints"]
        ):
            assert actual_ep.url == expected_ep["url"]
            assert actual_ep.concurrency == expected_ep["concurrency"]

    @given(config_dict=partial_config_dicts())
    @settings(max_examples=50)
    def test_absent_keys_get_documented_defaults(self, config_dict: dict):
        """When optional keys are absent, documented defaults are applied."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(config_dict, f)
            config_file = Path(f.name)

        result = load_config(config_file)

        assert isinstance(result, AppConfig)

        # Check each absent key gets its documented default
        if "host" not in config_dict:
            assert result.host == "0.0.0.0"
        else:
            assert result.host == config_dict["host"]

        if "port" not in config_dict:
            assert result.port == 8000
        else:
            assert result.port == config_dict["port"]

        if "event_data_path" not in config_dict:
            # Default "./data" is resolved relative to the config file's directory
            expected_default = (config_file.resolve().parent / Path("./data")).resolve()
            assert result.event_data_path == expected_default
            assert result.image_store_path == expected_default / "images"
            assert result.db_path == expected_default / "flight_cards.db"
        else:
            raw_path = Path(config_dict["event_data_path"])
            if raw_path.is_absolute():
                expected_data_path = raw_path
            else:
                expected_data_path = (config_file.resolve().parent / raw_path).resolve()
            assert result.event_data_path == expected_data_path
            assert result.image_store_path == result.event_data_path / "images"
            assert result.db_path == result.event_data_path / "flight_cards.db"

        if "event_name" not in config_dict:
            assert result.event_name == "Flight Card Scanner"
        else:
            assert result.event_name == config_dict["event_name"]

        if "event_date_range" not in config_dict:
            today = date.today()
            assert result.event_date_range == DateRange(start=today, end=today)
        else:
            dr = config_dict["event_date_range"]
            assert result.event_date_range.start == date.fromisoformat(dr["start"])
            assert result.event_date_range.end == date.fromisoformat(dr["end"])

        if "extraction_mode" not in config_dict:
            assert result.extraction_mode == "immediate"
        else:
            assert result.extraction_mode == config_dict["extraction_mode"]

        if "extraction_endpoints" not in config_dict:
            assert len(result.extraction_endpoints) == 1
            assert result.extraction_endpoints[0].url == "http://localhost:11434"
            assert result.extraction_endpoints[0].concurrency == 1
        else:
            expected = config_dict["extraction_endpoints"]
            assert len(result.extraction_endpoints) == len(expected)
            for actual_ep, expected_ep in zip(result.extraction_endpoints, expected):
                assert actual_ep.url == expected_ep["url"]
                assert actual_ep.concurrency == expected_ep["concurrency"]
