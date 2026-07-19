"""Unit tests for known_fliers_path config field."""
import json
import tempfile
from pathlib import Path

import pytest

from flight_card_scanner.config import load_config, AppConfig
from flight_card_scanner.exceptions import ConfigError


def _write_config(config_dict: dict) -> Path:
    """Write a config dict to a temp JSON file and return its path."""
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    json.dump(config_dict, f)
    f.close()
    return Path(f.name)


def _write_tsv(content: str) -> Path:
    """Write TSV content to a temp file and return its path."""
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".tsv", delete=False, encoding="utf-8"
    )
    f.write(content)
    f.close()
    return Path(f.name)


class TestKnownFliersConfig:
    """Tests for known_fliers_path configuration."""

    def test_absent_disables_feature(self):
        """When known_fliers_path is absent, feature is disabled."""
        config_path = _write_config({})
        result = load_config(config_path)
        assert result.known_fliers_path is None

    def test_known_fliers_path_with_existing_file(self):
        """When known_fliers_path is present and file exists, config loads successfully."""
        tsv_path = _write_tsv("Name\tNAR\tTRA\tLevel\nJohn\t12345\t\t3\n")
        config_path = _write_config({
            "known_fliers_path": str(tsv_path),
        })
        result = load_config(config_path)
        assert result.known_fliers_path == tsv_path

    def test_path_present_file_not_found_raises_config_error(self):
        """When known_fliers_path points to a non-existent file, raises ConfigError."""
        config_path = _write_config({
            "known_fliers_path": "/nonexistent/path/fliers.tsv",
        })
        with pytest.raises(ConfigError, match="Known fliers file not found"):
            load_config(config_path)

    def test_path_type_is_pathlib_path(self):
        """The known_fliers_path field should be a Path object."""
        tsv_path = _write_tsv("Name\tNAR\n")
        config_path = _write_config({
            "known_fliers_path": str(tsv_path),
        })
        result = load_config(config_path)
        assert isinstance(result.known_fliers_path, Path)
