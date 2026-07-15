"""Tests for Amazon Bedrock endpoint support."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from flight_card_scanner.config import (
    BedrockEndpointConfig,
    EndpointConfig,
    _parse_endpoint,
)
from flight_card_scanner.exceptions import (
    BedrockUnavailableError,
    ConfigError,
    ExtractionParseError,
)


# ---------------------------------------------------------------------------
# Config parsing tests
# ---------------------------------------------------------------------------


class TestParseEndpointBedrock:
    """Tests for _parse_endpoint with Bedrock configuration."""

    def test_bedrock_endpoint_created_with_valid_config(self):
        """A config with type='bedrock', region, and model_id produces BedrockEndpointConfig."""
        obj = {
            "type": "bedrock",
            "region": "us-west-2",
            "model_id": "us.anthropic.claude-sonnet-4-20250514-v1:0",
            "concurrency": 2,
        }
        result = _parse_endpoint(obj, 0)
        assert isinstance(result, BedrockEndpointConfig)
        assert result.type == "bedrock"
        assert result.region == "us-west-2"
        assert result.model_id == "us.anthropic.claude-sonnet-4-20250514-v1:0"
        assert result.concurrency == 2

    def test_bedrock_endpoint_default_concurrency(self):
        """Bedrock config with no concurrency defaults to 1."""
        obj = {
            "type": "bedrock",
            "region": "us-east-1",
            "model_id": "anthropic.claude-3-haiku-20240307-v1:0",
        }
        result = _parse_endpoint(obj, 0)
        assert isinstance(result, BedrockEndpointConfig)
        assert result.concurrency == 1

    def test_bedrock_endpoint_requires_region(self):
        """A bedrock config without region raises ConfigError."""
        obj = {
            "type": "bedrock",
            "model_id": "anthropic.claude-3-haiku-20240307-v1:0",
        }
        with pytest.raises(ConfigError, match="region must be a non-empty string"):
            _parse_endpoint(obj, 0)

    def test_bedrock_endpoint_requires_model_id(self):
        """A bedrock config without model_id raises ConfigError."""
        obj = {
            "type": "bedrock",
            "region": "us-west-2",
        }
        with pytest.raises(ConfigError, match="model_id must be a non-empty string"):
            _parse_endpoint(obj, 0)

    def test_bedrock_endpoint_empty_region_raises(self):
        """An empty region string raises ConfigError."""
        obj = {
            "type": "bedrock",
            "region": "",
            "model_id": "some-model",
        }
        with pytest.raises(ConfigError, match="region must be a non-empty string"):
            _parse_endpoint(obj, 0)

    def test_bedrock_endpoint_empty_model_id_raises(self):
        """An empty model_id string raises ConfigError."""
        obj = {
            "type": "bedrock",
            "region": "us-west-2",
            "model_id": "",
        }
        with pytest.raises(ConfigError, match="model_id must be a non-empty string"):
            _parse_endpoint(obj, 0)

    def test_bedrock_endpoint_invalid_concurrency(self):
        """Concurrency < 1 raises ConfigError for Bedrock endpoints."""
        obj = {
            "type": "bedrock",
            "region": "us-west-2",
            "model_id": "some-model",
            "concurrency": 0,
        }
        with pytest.raises(ConfigError, match="concurrency must be a positive integer"):
            _parse_endpoint(obj, 0)

    def test_unknown_type_raises_config_error(self):
        """An unknown endpoint type raises ConfigError."""
        obj = {
            "type": "openai",
            "url": "http://localhost:8080",
        }
        with pytest.raises(ConfigError, match="must be 'ollama' or 'bedrock'"):
            _parse_endpoint(obj, 0)


class TestParseEndpointOllama:
    """Tests for _parse_endpoint backward compatibility with Ollama."""

    def test_ollama_endpoint_without_type_field(self):
        """A config without a type field produces EndpointConfig (backward compat)."""
        obj = {"url": "http://localhost:11434", "concurrency": 2}
        result = _parse_endpoint(obj, 0)
        assert isinstance(result, EndpointConfig)
        assert result.url == "http://localhost:11434"
        assert result.concurrency == 2
        assert result.type == "ollama"

    def test_ollama_endpoint_with_explicit_type(self):
        """A config with type='ollama' produces EndpointConfig."""
        obj = {"type": "ollama", "url": "http://localhost:11434", "concurrency": 1}
        result = _parse_endpoint(obj, 0)
        assert isinstance(result, EndpointConfig)
        assert result.url == "http://localhost:11434"
        assert result.type == "ollama"

    def test_ollama_endpoint_still_requires_url(self):
        """An ollama config without url still raises ConfigError."""
        obj = {"type": "ollama", "concurrency": 1}
        with pytest.raises(ConfigError, match="url must be a non-empty string"):
            _parse_endpoint(obj, 0)


# ---------------------------------------------------------------------------
# ExtractionService initialization tests
# ---------------------------------------------------------------------------


class TestExtractionServiceBedrockInit:
    """Tests for ExtractionService properly handling Bedrock endpoints."""

    def _make_config(self):
        """Create a minimal AppConfig-like mock for testing."""
        from datetime import date
        from flight_card_scanner.config import AppConfig, DateRange

        return AppConfig(
            event_date_range=DateRange(start=date(2025, 1, 1), end=date(2025, 1, 3)),
            extraction_endpoints=[],
        )

    def test_semaphores_for_bedrock_endpoint(self):
        """ExtractionService creates semaphores keyed by bedrock:{region}:{model_id}."""
        from flight_card_scanner.services.extraction_service import ExtractionService

        config = self._make_config()
        bedrock_ep = BedrockEndpointConfig(
            region="us-west-2",
            model_id="anthropic.claude-3-haiku-20240307-v1:0",
            concurrency=3,
        )
        service = ExtractionService(
            config=config,
            session_factory=MagicMock(),
            extraction_endpoints=[bedrock_ep],
        )
        key = "bedrock:us-west-2:anthropic.claude-3-haiku-20240307-v1:0"
        assert key in service._endpoint_semaphores
        assert service._endpoint_semaphores[key]._value == 3

    def test_semaphores_for_mixed_endpoints(self):
        """ExtractionService handles both Ollama and Bedrock endpoints."""
        from flight_card_scanner.services.extraction_service import ExtractionService

        config = self._make_config()
        ollama_ep = EndpointConfig(url="http://localhost:11434", concurrency=2)
        bedrock_ep = BedrockEndpointConfig(
            region="us-east-1",
            model_id="some-model-id",
            concurrency=1,
        )
        service = ExtractionService(
            config=config,
            session_factory=MagicMock(),
            extraction_endpoints=[ollama_ep, bedrock_ep],
        )
        assert "http://localhost:11434" in service._endpoint_semaphores
        assert "bedrock:us-east-1:some-model-id" in service._endpoint_semaphores
        assert service._endpoint_semaphores["http://localhost:11434"]._value == 2
        assert service._endpoint_semaphores["bedrock:us-east-1:some-model-id"]._value == 1


# ---------------------------------------------------------------------------
# _call_bedrock tests
# ---------------------------------------------------------------------------


class TestCallBedrock:
    """Tests for the _call_bedrock method with mocked boto3."""

    def _make_service(self, tmp_path):
        """Create an ExtractionService with a temporary image store."""
        from datetime import date
        from flight_card_scanner.config import AppConfig, DateRange

        config = AppConfig(
            event_data_path=tmp_path,
            event_date_range=DateRange(start=date(2025, 7, 1), end=date(2025, 7, 5)),
            extraction_endpoints=[],
        )
        # Create the images directory
        (tmp_path / "images").mkdir(exist_ok=True)
        return config

    def _create_test_image(self, tmp_path):
        """Create a small test JPEG image and return its relative path."""
        from PIL import Image
        from io import BytesIO

        img = Image.new("RGB", (100, 100), color="red")
        buf = BytesIO()
        img.save(buf, format="JPEG")
        image_bytes = buf.getvalue()

        images_dir = tmp_path / "images"
        images_dir.mkdir(exist_ok=True)
        image_file = images_dir / "test_card.jpg"
        image_file.write_bytes(image_bytes)
        return "test_card.jpg"

    @pytest.mark.asyncio
    async def test_call_bedrock_parses_valid_response(self, tmp_path):
        """_call_bedrock correctly parses a valid JSON response from Bedrock."""
        from flight_card_scanner.services.extraction_service import ExtractionService

        config = self._make_service(tmp_path)
        image_path = self._create_test_image(tmp_path)

        service = ExtractionService(
            config=config,
            session_factory=MagicMock(),
            extraction_endpoints=[],
        )

        # Create a valid FlightCardExtraction JSON response
        valid_response = json.dumps({
            "flight_date_raw": "Saturday",
            "flier_name": "John Doe",
            "rocket_name": "Big Red",
            "motors": [],
        })

        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"text": valid_response}]
                }
            }
        }

        result = await service._call_bedrock(
            mock_client, "anthropic.claude-3-haiku-20240307-v1:0", image_path, 1
        )

        assert result.flier_name == "John Doe"
        assert result.rocket_name == "Big Red"
        assert result.flight_date_raw == "Saturday"
        mock_client.converse.assert_called_once()

    @pytest.mark.asyncio
    async def test_call_bedrock_raises_on_api_failure(self, tmp_path):
        """_call_bedrock raises BedrockUnavailableError on API failure."""
        from flight_card_scanner.services.extraction_service import ExtractionService
        from botocore.exceptions import ClientError

        config = self._make_service(tmp_path)
        image_path = self._create_test_image(tmp_path)

        service = ExtractionService(
            config=config,
            session_factory=MagicMock(),
            extraction_endpoints=[],
        )

        mock_client = MagicMock()
        mock_client.converse.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "Converse",
        )

        with pytest.raises(BedrockUnavailableError, match="Bedrock API call failed"):
            await service._call_bedrock(
                mock_client, "some-model", image_path, 1
            )

    @pytest.mark.asyncio
    async def test_call_bedrock_raises_on_empty_content(self, tmp_path):
        """_call_bedrock raises ExtractionParseError on empty response content."""
        from flight_card_scanner.services.extraction_service import ExtractionService

        config = self._make_service(tmp_path)
        image_path = self._create_test_image(tmp_path)

        service = ExtractionService(
            config=config,
            session_factory=MagicMock(),
            extraction_endpoints=[],
        )

        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"text": ""}]
                }
            }
        }

        with pytest.raises(ExtractionParseError, match="empty content"):
            await service._call_bedrock(
                mock_client, "some-model", image_path, 1
            )

    @pytest.mark.asyncio
    async def test_call_bedrock_strips_think_blocks(self, tmp_path):
        """_call_bedrock strips <think> blocks before parsing."""
        from flight_card_scanner.services.extraction_service import ExtractionService

        config = self._make_service(tmp_path)
        image_path = self._create_test_image(tmp_path)

        service = ExtractionService(
            config=config,
            session_factory=MagicMock(),
            extraction_endpoints=[],
        )

        valid_json = json.dumps({
            "flight_date_raw": None,
            "flier_name": "Jane Smith",
            "rocket_name": None,
            "motors": [],
        })

        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"text": f"<think>some reasoning</think>{valid_json}"}]
                }
            }
        }

        result = await service._call_bedrock(
            mock_client, "some-model", image_path, 1
        )
        assert result.flier_name == "Jane Smith"

    @pytest.mark.asyncio
    async def test_call_bedrock_handles_markdown_fenced_json(self, tmp_path):
        """_call_bedrock extracts JSON from markdown code fences."""
        from flight_card_scanner.services.extraction_service import ExtractionService

        config = self._make_service(tmp_path)
        image_path = self._create_test_image(tmp_path)

        service = ExtractionService(
            config=config,
            session_factory=MagicMock(),
            extraction_endpoints=[],
        )

        valid_json = json.dumps({
            "flight_date_raw": None,
            "flier_name": "Bob Builder",
            "rocket_name": "Falcon",
            "motors": [],
        })

        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"text": f"```json\n{valid_json}\n```"}]
                }
            }
        }

        result = await service._call_bedrock(
            mock_client, "some-model", image_path, 1
        )
        assert result.flier_name == "Bob Builder"
        assert result.rocket_name == "Falcon"

    @pytest.mark.asyncio
    async def test_call_bedrock_uses_extraction_prompt(self, tmp_path):
        """_call_bedrock passes the shared EXTRACTION_PROMPT to Bedrock."""
        from flight_card_scanner.services.extraction_service import (
            EXTRACTION_PROMPT,
            ExtractionService,
        )

        config = self._make_service(tmp_path)
        image_path = self._create_test_image(tmp_path)

        service = ExtractionService(
            config=config,
            session_factory=MagicMock(),
            extraction_endpoints=[],
        )

        valid_json = json.dumps({
            "flight_date_raw": None,
            "flier_name": "Test",
            "rocket_name": None,
            "motors": [],
        })

        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"text": valid_json}]
                }
            }
        }

        await service._call_bedrock(mock_client, "some-model", image_path, 1)

        # Verify the prompt was passed in the message content
        call_args = mock_client.converse.call_args
        messages = call_args[1]["messages"] if "messages" in call_args[1] else call_args[0][0]
        content_blocks = messages[0]["content"]
        text_block = next(b for b in content_blocks if "text" in b)
        # The prompt should contain key phrases from EXTRACTION_PROMPT
        assert "rocketry flight card" in text_block["text"]
        assert "Extract every readable field" in text_block["text"]


    @pytest.mark.asyncio
    async def test_call_bedrock_raises_parse_error_on_invalid_json(self, tmp_path):
        """_call_bedrock raises ExtractionParseError when response is not valid JSON."""
        from flight_card_scanner.services.extraction_service import ExtractionService

        config = self._make_service(tmp_path)
        image_path = self._create_test_image(tmp_path)

        service = ExtractionService(
            config=config,
            session_factory=MagicMock(),
            extraction_endpoints=[],
        )

        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"text": "This is not valid JSON at all"}]
                }
            }
        }

        with pytest.raises(ExtractionParseError, match="Invalid JSON from Bedrock"):
            await service._call_bedrock(
                mock_client, "some-model", image_path, 1
            )

    @pytest.mark.asyncio
    async def test_call_bedrock_writes_request_sidecar(self, tmp_path):
        """_call_bedrock writes a .request sidecar file with the prompt payload."""
        from flight_card_scanner.services.extraction_service import ExtractionService

        config = self._make_service(tmp_path)
        image_path = self._create_test_image(tmp_path)

        service = ExtractionService(
            config=config,
            session_factory=MagicMock(),
            extraction_endpoints=[],
        )

        valid_json = json.dumps({
            "flight_date_raw": None,
            "flier_name": "Test",
            "rocket_name": None,
            "motors": [],
        })

        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"text": valid_json}]
                }
            }
        }

        await service._call_bedrock(mock_client, "some-model", image_path, 1)

        # Check that .request sidecar was written
        request_path = tmp_path / "images" / "test_card.request"
        assert request_path.exists()
        request_content = json.loads(request_path.read_text())
        assert request_content["modelId"] == "some-model"
        assert "rocketry flight card" in request_content["messages"][0]["content"][1]["text"]

    @pytest.mark.asyncio
    async def test_call_bedrock_writes_json_sidecar(self, tmp_path):
        """_call_bedrock writes a .json sidecar file with the raw response."""
        from flight_card_scanner.services.extraction_service import ExtractionService

        config = self._make_service(tmp_path)
        image_path = self._create_test_image(tmp_path)

        service = ExtractionService(
            config=config,
            session_factory=MagicMock(),
            extraction_endpoints=[],
        )

        valid_json = json.dumps({
            "flight_date_raw": None,
            "flier_name": "Test",
            "rocket_name": None,
            "motors": [],
        })

        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"text": valid_json}]
                }
            },
            "stopReason": "end_turn",
            "usage": {"inputTokens": 100, "outputTokens": 50},
        }

        await service._call_bedrock(mock_client, "some-model", image_path, 1)

        # Check that .json sidecar was written
        json_path = tmp_path / "images" / "test_card.json"
        assert json_path.exists()
        json_content = json.loads(json_path.read_text())
        assert json_content["stopReason"] == "end_turn"
        assert json_content["usage"]["inputTokens"] == 100


# ---------------------------------------------------------------------------
# BedrockEndpointConfig dataclass tests
# ---------------------------------------------------------------------------


class TestBedrockEndpointConfig:
    """Tests for the BedrockEndpointConfig dataclass."""

    def test_default_values(self):
        """BedrockEndpointConfig has correct defaults."""
        ep = BedrockEndpointConfig(region="us-west-2", model_id="some-model")
        assert ep.type == "bedrock"
        assert ep.concurrency == 1

    def test_no_url_attribute(self):
        """BedrockEndpointConfig does not have a url field."""
        ep = BedrockEndpointConfig(region="us-west-2", model_id="some-model")
        # It should not have a 'url' attribute (it's not defined in the dataclass)
        assert not hasattr(ep, "url") or "url" not in ep.__dataclass_fields__


# ---------------------------------------------------------------------------
# BedrockUnavailableError tests
# ---------------------------------------------------------------------------


class TestBedrockUnavailableError:
    """Tests for the BedrockUnavailableError exception."""

    def test_is_flight_card_scanner_error(self):
        """BedrockUnavailableError inherits from FlightCardScannerError."""
        from flight_card_scanner.exceptions import FlightCardScannerError

        err = BedrockUnavailableError("test error")
        assert isinstance(err, FlightCardScannerError)

    def test_message_preserved(self):
        """BedrockUnavailableError preserves the error message."""
        err = BedrockUnavailableError("Bedrock API call failed: timeout")
        assert "Bedrock API call failed: timeout" in str(err)
