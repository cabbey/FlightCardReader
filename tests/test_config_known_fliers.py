"""Unit tests for known_fliers_path and flier_match_model config fields."""
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
    """Tests for known_fliers_path and flier_match_model configuration."""

    def test_both_absent_disables_feature(self):
        """When both known_fliers_path and flier_match_model are absent, feature is disabled."""
        config_path = _write_config({})
        result = load_config(config_path)
        assert result.known_fliers_path is None
        assert result.flier_match_model is None

    def test_both_present_with_existing_file(self):
        """When both fields are present and file exists, config loads successfully."""
        tsv_path = _write_tsv("Name\tNAR\tTRA\tLevel\nJohn\t12345\t\t3\n")
        config_path = _write_config({
            "known_fliers_path": str(tsv_path),
            "flier_match_model": "qwen2.5:7b",
        })
        result = load_config(config_path)
        assert result.known_fliers_path == tsv_path
        assert result.flier_match_model == "qwen2.5:7b"

    def test_path_present_model_absent_raises_config_error(self):
        """When known_fliers_path is set but flier_match_model is missing, raises ConfigError."""
        tsv_path = _write_tsv("Name\tNAR\tTRA\tLevel\n")
        config_path = _write_config({
            "known_fliers_path": str(tsv_path),
        })
        with pytest.raises(ConfigError, match="flier_match_model.*required"):
            load_config(config_path)

    def test_path_present_file_not_found_raises_config_error(self):
        """When known_fliers_path points to a non-existent file, raises ConfigError."""
        config_path = _write_config({
            "known_fliers_path": "/nonexistent/path/fliers.tsv",
            "flier_match_model": "qwen2.5:7b",
        })
        with pytest.raises(ConfigError, match="Known fliers file not found"):
            load_config(config_path)

    def test_path_type_is_pathlib_path(self):
        """The known_fliers_path field should be a Path object."""
        tsv_path = _write_tsv("Name\tNAR\n")
        config_path = _write_config({
            "known_fliers_path": str(tsv_path),
            "flier_match_model": "llama3.1:8b",
        })
        result = load_config(config_path)
        assert isinstance(result.known_fliers_path, Path)

    def test_model_only_without_path_no_error(self):
        """When flier_match_model is set but known_fliers_path is absent, no validation error."""
        config_path = _write_config({
            "flier_match_model": "qwen2.5:7b",
        })
        # No ConfigError is raised; the feature is just not fully configured
        result = load_config(config_path)
        assert result.known_fliers_path is None
        assert result.flier_match_model == "qwen2.5:7b"
