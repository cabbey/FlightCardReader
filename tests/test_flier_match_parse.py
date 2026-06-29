"""Tests for FlierMatchService.parse_response() and get_row_by_line_number()."""

import tempfile
from pathlib import Path

import pytest

from flight_card_scanner.services.flier_match_service import FlierMatchService


@pytest.fixture
def service_with_rows() -> FlierMatchService:
    """Create a FlierMatchService with a loaded TSV containing 3 data rows."""
    tsv_content = "Name\tNAR\tTRA\tLevel\nJohn Smith\t12345\t\t3\nJane Doe\t\t54321\t2\nBob Johnson\t67890\t11111\t1\n"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".tsv", delete=False, encoding="utf-8"
    ) as f:
        f.write(tsv_content)
        path = Path(f.name)
    svc = FlierMatchService(known_fliers_path=path, flier_match_model="test-model")
    svc.load()
    return svc


class TestParseResponse:
    """Tests for parse_response()."""

    def test_parses_simple_integer(self, service_with_rows: FlierMatchService) -> None:
        assert service_with_rows.parse_response("3") == 3

    def test_parses_zero(self, service_with_rows: FlierMatchService) -> None:
        assert service_with_rows.parse_response("0") == 0

    def test_strips_whitespace(self, service_with_rows: FlierMatchService) -> None:
        assert service_with_rows.parse_response("  5  ") == 5

    def test_strips_newlines(self, service_with_rows: FlierMatchService) -> None:
        assert service_with_rows.parse_response("\n2\n") == 2

    def test_strips_mixed_whitespace(
        self, service_with_rows: FlierMatchService
    ) -> None:
        assert service_with_rows.parse_response(" \t\n 4 \n\t ") == 4

    def test_raises_on_non_integer(
        self, service_with_rows: FlierMatchService
    ) -> None:
        with pytest.raises(ValueError):
            service_with_rows.parse_response("abc")

    def test_raises_on_empty_string(
        self, service_with_rows: FlierMatchService
    ) -> None:
        with pytest.raises(ValueError):
            service_with_rows.parse_response("")

    def test_raises_on_float(self, service_with_rows: FlierMatchService) -> None:
        with pytest.raises(ValueError):
            service_with_rows.parse_response("3.5")

    def test_raises_on_mixed_text_and_number(
        self, service_with_rows: FlierMatchService
    ) -> None:
        with pytest.raises(ValueError):
            service_with_rows.parse_response("line 3")


class TestGetRowByLineNumber:
    """Tests for get_row_by_line_number()."""

    def test_line_2_returns_first_data_row(
        self, service_with_rows: FlierMatchService
    ) -> None:
        row = service_with_rows.get_row_by_line_number(2)
        assert row is not None
        assert row["Name"] == "John Smith"
        assert row["NAR"] == "12345"

    def test_line_3_returns_second_data_row(
        self, service_with_rows: FlierMatchService
    ) -> None:
        row = service_with_rows.get_row_by_line_number(3)
        assert row is not None
        assert row["Name"] == "Jane Doe"
        assert row["TRA"] == "54321"

    def test_line_4_returns_third_data_row(
        self, service_with_rows: FlierMatchService
    ) -> None:
        row = service_with_rows.get_row_by_line_number(4)
        assert row is not None
        assert row["Name"] == "Bob Johnson"

    def test_line_1_returns_none(
        self, service_with_rows: FlierMatchService
    ) -> None:
        # Line 1 is the header, not a data row
        assert service_with_rows.get_row_by_line_number(1) is None

    def test_line_0_returns_none(
        self, service_with_rows: FlierMatchService
    ) -> None:
        assert service_with_rows.get_row_by_line_number(0) is None

    def test_negative_line_returns_none(
        self, service_with_rows: FlierMatchService
    ) -> None:
        assert service_with_rows.get_row_by_line_number(-1) is None

    def test_line_beyond_rows_returns_none(
        self, service_with_rows: FlierMatchService
    ) -> None:
        # Only 3 data rows, so line 5 (index 3) is out of bounds
        assert service_with_rows.get_row_by_line_number(5) is None
