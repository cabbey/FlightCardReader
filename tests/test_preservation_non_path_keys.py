# Preservation Property Test: Non-Path Config Keys Unchanged
"""
Property-based test verifying that non-path config keys (host, port, event_name,
event_date_range, extraction_mode, extraction_endpoints) load correctly and are
unaffected by the stale keys bug.

These tests should PASS on both unfixed and fixed code, confirming that the bug
only affects path-related keys (image_store_path, db_path, event_data_path).

**Validates: Requirements 3.1, 3.2, 3.3**
"""
import json
import tempfile
from datetime import date
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from flight_card_scanner.config import load_config, AppConfig, EndpointConfig, DateRange


# --- Strategies (same as in test_config_loading_fidelity.py) ---

# Valid host strings
hosts = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters=".-_"),
    min_size=1,
    max_size=50,
)

# Valid port integers
ports = st.integers(min_value=1, max_value=65535)

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


@st.composite
def non_path_config_dicts(draw):
    """Generate a config dict with all non-path keys present."""
    start, end = draw(date_ranges)
    return {
        "host": draw(hosts),
        "port": draw(ports),
        "event_name": draw(event_names),
        "event_date_range": {
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
        "extraction_mode": draw(extraction_modes),
        "extraction_endpoints": draw(endpoint_lists),
    }


class TestPreservationNonPathKeys:
    """Property 2: Preservation - Non-path config keys load correctly."""

    @given(config_dict=non_path_config_dicts())
    @settings(max_examples=50)
    def test_non_path_keys_load_correctly(self, config_dict: dict):
        """
        Property: When non-path config keys are provided, load_config returns
        an AppConfig where each non-path field matches the config dict value.

        This confirms baseline behavior that must be preserved during the fix.
        """
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(config_dict, f)
            config_file = Path(f.name)

        result = load_config(config_file)

        assert isinstance(result, AppConfig)

        # Host matches
        assert result.host == config_dict["host"]

        # Port matches
        assert result.port == config_dict["port"]

        # Event name matches
        assert result.event_name == config_dict["event_name"]

        # Date range start/end match
        dr = config_dict["event_date_range"]
        assert result.event_date_range.start == date.fromisoformat(dr["start"])
        assert result.event_date_range.end == date.fromisoformat(dr["end"])

        # Extraction mode matches
        assert result.extraction_mode == config_dict["extraction_mode"]

        # Extraction endpoints match
        assert len(result.extraction_endpoints) == len(config_dict["extraction_endpoints"])
        for actual_ep, expected_ep in zip(
            result.extraction_endpoints, config_dict["extraction_endpoints"]
        ):
            assert actual_ep.url == expected_ep["url"]
            assert actual_ep.concurrency == expected_ep["concurrency"]

    @given(config_dict=non_path_config_dicts())
    @settings(max_examples=50)
    def test_non_path_keys_defaults_when_absent(self, config_dict: dict):
        """
        Property: When non-path config keys are individually absent, their
        documented defaults are applied correctly.

        For each key, we remove it from the config and verify the default.
        This confirms the default behavior that must be preserved.
        """
        # Test each key being absent one at a time
        for key_to_remove in list(config_dict.keys()):
            partial_config = {k: v for k, v in config_dict.items() if k != key_to_remove}

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, encoding="utf-8"
            ) as f:
                json.dump(partial_config, f)
                config_file = Path(f.name)

            result = load_config(config_file)
            assert isinstance(result, AppConfig)

            if key_to_remove == "host":
                assert result.host == "0.0.0.0"
            elif key_to_remove == "port":
                assert result.port == 8000
            elif key_to_remove == "event_name":
                assert result.event_name == "Flight Card Scanner"
            elif key_to_remove == "event_date_range":
                today = date.today()
                assert result.event_date_range == DateRange(start=today, end=today)
            elif key_to_remove == "extraction_mode":
                assert result.extraction_mode == "immediate"
            elif key_to_remove == "extraction_endpoints":
                assert len(result.extraction_endpoints) == 1
                assert result.extraction_endpoints[0].url == "http://localhost:11434"
                assert result.extraction_endpoints[0].concurrency == 1
