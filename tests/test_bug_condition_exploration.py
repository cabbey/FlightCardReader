# Bug Condition Exploration: Stale image_store_path/db_path Config Keys
"""
Property-based exploration test to confirm the bug condition described in the
bugfix spec for config-test-stale-keys.

This test encodes the CORRECT expected behavior: config dicts should use
`event_data_path` (not `image_store_path`/`db_path`), and the derived
properties should be computed from `event_data_path`.

This test should PASS against the current (correct) code. The existing tests
in test_config_loading_fidelity.py should FAIL because they still reference
stale keys.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6**
"""
import json
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flight_card_scanner.config import load_config, AppConfig


# Valid filesystem path strings (relative or absolute, non-empty)
path_strings = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="/._ -"),
    min_size=1,
    max_size=100,
).filter(lambda s: s.strip() != "")


@st.composite
def config_dicts_with_event_data_path(draw):
    """Generate a config dict with event_data_path (the correct key)."""
    return {
        "event_data_path": draw(path_strings),
    }


class TestBugConditionExploration:
    """Exploration test: event_data_path drives image_store_path and db_path."""

    @given(config_dict=config_dicts_with_event_data_path())
    @settings(max_examples=50)
    def test_event_data_path_drives_derived_properties(self, config_dict: dict):
        """
        Property: When event_data_path is provided in config, load_config returns
        an AppConfig where:
        - result.event_data_path == Path(config_dict["event_data_path"])
        - result.image_store_path == result.event_data_path / "images"
        - result.db_path == result.event_data_path / "flight_cards.db"

        This encodes the correct behavior after the refactor. The existing tests
        incorrectly treat image_store_path and db_path as independent config keys.
        """
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(config_dict, f)
            config_file = Path(f.name)

        result = load_config(config_file)

        assert isinstance(result, AppConfig)
        assert result.event_data_path == Path(config_dict["event_data_path"])
        assert result.image_store_path == result.event_data_path / "images"
        assert result.db_path == result.event_data_path / "flight_cards.db"
