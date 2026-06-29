"""Tests for FlierMatchService.match_flier() async method."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from flight_card_scanner.services.flier_match_service import (
    FlierMatchResult,
    FlierMatchService,
)


@pytest.fixture
def service() -> FlierMatchService:
    """Create a FlierMatchService with a loaded TSV containing 3 data rows."""
    tsv_content = (
        "Name\tNAR\tTRA\tLevel\n"
        "John Smith\t12345\t\t3\n"
        "Jane Doe\t\t54321\t2\n"
        "Bob Johnson\t67890\t11111\t1\n"
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".tsv", delete=False, encoding="utf-8"
    ) as f:
        f.write(tsv_content)
        path = Path(f.name)
    svc = FlierMatchService(known_fliers_path=path, flier_match_model="test-model")
    svc.load()
    return svc


def _mock_response(content: str, status_code: int = 200) -> httpx.Response:
    """Create a mock httpx.Response with JSON body."""
    import json

    body = json.dumps({"message": {"content": content}}).encode()
    return httpx.Response(
        status_code=status_code,
        content=body,
        headers={"content-type": "application/json"},
        request=httpx.Request("POST", "http://test/api/chat"),
    )


class TestMatchFlierSuccess:
    """Tests for successful match flow."""

    @pytest.mark.anyio
    async def test_successful_match_returns_row(self, service: FlierMatchService) -> None:
        """When LLM returns a valid line number, match_flier returns matched=True with row data."""
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response("2")

        result = await service.match_flier(
            client=mock_client,
            flier_name="Jon Smith",
            club="NAR",
            member_number="12345",
            cert_level=3,
        )

        assert result.matched is True
        assert result.line_number == 2
        assert result.row_data is not None
        assert result.row_data["Name"] == "John Smith"
        assert result.row_data["NAR"] == "12345"
        assert result.error is None

    @pytest.mark.anyio
    async def test_match_last_row(self, service: FlierMatchService) -> None:
        """When LLM returns the last valid line number."""
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response("4")

        result = await service.match_flier(
            client=mock_client,
            flier_name="Bob",
            club=None,
            member_number=None,
            cert_level=None,
        )

        assert result.matched is True
        assert result.line_number == 4
        assert result.row_data["Name"] == "Bob Johnson"
        assert result.error is None


class TestMatchFlierNoMatch:
    """Tests for no-match scenarios."""

    @pytest.mark.anyio
    async def test_zero_means_no_match(self, service: FlierMatchService) -> None:
        """When LLM returns 0, match_flier returns matched=False with no error."""
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response("0")

        result = await service.match_flier(
            client=mock_client,
            flier_name="Unknown Person",
            club=None,
            member_number=None,
            cert_level=None,
        )

        assert result.matched is False
        assert result.line_number == 0
        assert result.row_data is None
        assert result.error is None

    @pytest.mark.anyio
    async def test_out_of_bounds_line_number(self, service: FlierMatchService) -> None:
        """When LLM returns a line number beyond the data, returns matched=False."""
        mock_client = AsyncMock()
        # 3 data rows → lines 2-4 valid; line 5 is out of bounds
        mock_client.post.return_value = _mock_response("5")

        result = await service.match_flier(
            client=mock_client,
            flier_name="Someone",
            club=None,
            member_number=None,
            cert_level=None,
        )

        assert result.matched is False
        assert result.line_number == 5
        assert result.row_data is None
        assert result.error is None

    @pytest.mark.anyio
    async def test_line_1_header_is_out_of_bounds(self, service: FlierMatchService) -> None:
        """Line 1 is the header row, not a data row — treated as out of bounds."""
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response("1")

        result = await service.match_flier(
            client=mock_client,
            flier_name="Someone",
            club=None,
            member_number=None,
            cert_level=None,
        )

        assert result.matched is False
        assert result.line_number == 1
        assert result.row_data is None
        assert result.error is None


class TestMatchFlierErrors:
    """Tests for error handling in match_flier."""

    @pytest.mark.anyio
    async def test_http_status_error(self, service: FlierMatchService) -> None:
        """HTTP errors from Ollama return a result with error set."""
        mock_client = AsyncMock()
        error_response = httpx.Response(
            status_code=500,
            request=httpx.Request("POST", "http://test/api/chat"),
        )
        mock_client.post.return_value = error_response
        # Simulate raise_for_status behavior by making it raise
        mock_client.post.side_effect = httpx.HTTPStatusError(
            "Internal Server Error",
            request=httpx.Request("POST", "http://test/api/chat"),
            response=error_response,
        )

        result = await service.match_flier(
            client=mock_client,
            flier_name="Test",
            club=None,
            member_number=None,
            cert_level=None,
        )

        assert result.matched is False
        assert result.line_number == 0
        assert result.row_data is None
        assert result.error is not None
        assert "Internal Server Error" in result.error

    @pytest.mark.anyio
    async def test_request_error(self, service: FlierMatchService) -> None:
        """Network/connection errors return a result with error set."""
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.RequestError(
            "Connection refused",
            request=httpx.Request("POST", "http://test/api/chat"),
        )

        result = await service.match_flier(
            client=mock_client,
            flier_name="Test",
            club=None,
            member_number=None,
            cert_level=None,
        )

        assert result.matched is False
        assert result.line_number == 0
        assert result.row_data is None
        assert result.error is not None

    @pytest.mark.anyio
    async def test_non_integer_response(self, service: FlierMatchService) -> None:
        """Non-integer LLM response returns error with raw content."""
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response("I think the answer is line 3")

        result = await service.match_flier(
            client=mock_client,
            flier_name="Test",
            club=None,
            member_number=None,
            cert_level=None,
        )

        assert result.matched is False
        assert result.line_number == 0
        assert result.row_data is None
        assert result.error is not None
        assert "Failed to parse LLM response" in result.error
        assert "I think the answer is line 3" in result.error

    @pytest.mark.anyio
    async def test_empty_response(self, service: FlierMatchService) -> None:
        """Empty LLM response is a parse error."""
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response("")

        result = await service.match_flier(
            client=mock_client,
            flier_name="Test",
            club=None,
            member_number=None,
            cert_level=None,
        )

        assert result.matched is False
        assert result.line_number == 0
        assert result.error is not None
        assert "Failed to parse LLM response" in result.error
