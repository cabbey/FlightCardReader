"""Tests for back image scanning flow (FEAT-002).

Verifies:
- POST /api/scan with both card_image and back_image saves both files
- POST /api/scan with only card_image works as before (no back_image_path)
- back_image_path is stored on the record when back image is provided
- Invalid back image type is rejected
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from starlette.middleware.base import BaseHTTPMiddleware

from flight_card_scanner.database import get_db
from flight_card_scanner.models import FlightRecord
from flight_card_scanner.routers import scan
from flight_card_scanner.routers.scan import router


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _FakeUser:
    """Minimal user object to satisfy require_role checks in tests."""

    def __init__(self, role: str = "data_entry"):
        self.role = role
        self.email = "scanner@example.com"
        self.display_name = "Test Scanner"


class _MockAuthMiddleware(BaseHTTPMiddleware):
    """Middleware that attaches a fake data_entry user to request.state."""

    async def dispatch(self, request: Request, call_next):
        request.state.user = _FakeUser("data_entry")
        request.state.session_token = None
        request.state.clear_session_cookie = False
        return await call_next(request)


class _FakeConfig:
    """Minimal config object for scan router tests."""

    def __init__(self, image_store_path: Path):
        self.image_store_path = image_store_path
        self.event_name = "Test Event"
        from datetime import date
        from flight_card_scanner.config import DateRange
        self.event_date_range = DateRange(
            start=date(2025, 4, 1), end=date(2025, 4, 5)
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def image_store(tmp_path):
    """Create a temporary image store directory."""
    store = tmp_path / "images"
    store.mkdir()
    return store


@pytest.fixture
def fake_config(image_store):
    """Create a fake AppConfig pointing at the tmp image store."""
    return _FakeConfig(image_store)


@pytest.fixture
def mock_extraction_service():
    """Create a mock ExtractionService that no-ops enqueue."""
    svc = AsyncMock()
    svc.enqueue = AsyncMock()
    return svc


@pytest.fixture
def mock_db(image_store):
    """Create a mock async DB session that simulates record creation."""
    db = AsyncMock()
    _record_counter = [0]

    async def _commit():
        pass

    async def _refresh(obj):
        _record_counter[0] += 1
        obj.id = _record_counter[0]

    db.commit = _commit
    db.refresh = _refresh
    db.add = MagicMock()
    return db


@pytest.fixture(autouse=True)
def configure_scan_router(fake_config, mock_extraction_service):
    """Wire up the scan router with fake deps."""
    scan.configure(
        config=fake_config,
        extraction_service=mock_extraction_service,
    )
    yield
    scan._config = None
    scan._extraction_service = None


@pytest.fixture
def app(mock_db, fake_config, mock_extraction_service):
    """Create a FastAPI test app with the scan router."""
    test_app = FastAPI()
    test_app.add_middleware(_MockAuthMiddleware)
    test_app.include_router(router)

    async def override_get_db():
        yield mock_db

    test_app.dependency_overrides[get_db] = override_get_db
    return test_app


@pytest.fixture
async def client(app):
    """Async HTTP client for testing."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_submit_front_only(client, image_store, mock_extraction_service):
    """POST /api/scan with only card_image works without back_image."""
    files = {"card_image": ("card.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 100, "image/jpeg")}
    response = await client.post("/api/scan", files=files)

    assert response.status_code == 201
    data = response.json()
    assert "record_id" in data

    # Only front image file should exist in the store (plus .jsonl history log)
    image_files = [f for f in image_store.iterdir() if f.suffix in (".jpg", ".jpeg", ".png")]
    assert len(image_files) == 1
    assert "-back" not in image_files[0].name

    # Extraction should have been called
    mock_extraction_service.enqueue.assert_called_once()


@pytest.mark.anyio
async def test_submit_front_and_back(client, image_store, mock_extraction_service):
    """POST /api/scan with both card_image and back_image saves both files."""
    files = {
        "card_image": ("card.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 100, "image/jpeg"),
        "back_image": ("card-back.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 50, "image/jpeg"),
    }
    response = await client.post("/api/scan", files=files)

    assert response.status_code == 201
    data = response.json()
    assert "record_id" in data

    # Both image files should exist in the store (ignore .jsonl history)
    image_files = sorted(
        f.name for f in image_store.iterdir() if f.suffix in (".jpg", ".jpeg", ".png")
    )
    assert len(image_files) == 2

    # One should have -back in the name
    back_files = [f for f in image_files if "-back" in f]
    front_files = [f for f in image_files if "-back" not in f]
    assert len(back_files) == 1
    assert len(front_files) == 1

    # The back file should be named based on the front file
    front_stem = front_files[0].rsplit(".", 1)[0]
    assert back_files[0] == f"{front_stem}-back.jpg"


@pytest.mark.anyio
async def test_back_image_path_stored_on_record(client, mock_db, image_store):
    """back_image_path is set on the FlightRecord when back image is provided."""
    files = {
        "card_image": ("card.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 100, "image/jpeg"),
        "back_image": ("card-back.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 50, "image/png"),
    }
    response = await client.post("/api/scan", files=files)

    assert response.status_code == 201

    # The record that was added to the session should have back_image_path set
    # Find the FlightRecord that was added
    added_obj = mock_db.add.call_args[0][0]
    assert isinstance(added_obj, FlightRecord)
    # After commit, back_image_path is set on the record
    assert added_obj.back_image_path is not None
    assert "-back" in added_obj.back_image_path


@pytest.mark.anyio
async def test_no_back_image_path_when_not_provided(client, mock_db, image_store):
    """back_image_path is NOT set when no back image is provided."""
    files = {"card_image": ("card.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 100, "image/jpeg")}
    response = await client.post("/api/scan", files=files)

    assert response.status_code == 201

    # The record should not have back_image_path set
    added_obj = mock_db.add.call_args[0][0]
    assert isinstance(added_obj, FlightRecord)
    assert added_obj.back_image_path is None


@pytest.mark.anyio
async def test_invalid_back_image_type_rejected(client, image_store):
    """POST /api/scan rejects back_image with unsupported file type."""
    files = {
        "card_image": ("card.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 100, "image/jpeg"),
        "back_image": ("card-back.gif", b"GIF89a" + b"\x00" * 50, "image/gif"),
    }
    response = await client.post("/api/scan", files=files)

    assert response.status_code == 400
    assert "back image" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_back_image_content_persisted(client, image_store):
    """The back image file content is stored correctly."""
    back_content = b"\xff\xd8\xff\xe0" + b"\xAB" * 200
    files = {
        "card_image": ("card.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 100, "image/jpeg"),
        "back_image": ("card-back.jpg", back_content, "image/jpeg"),
    }
    response = await client.post("/api/scan", files=files)

    assert response.status_code == 201

    # Find the back image file and verify its content
    back_files = [f for f in image_store.iterdir() if "-back" in f.name]
    assert len(back_files) == 1
    assert back_files[0].read_bytes() == back_content
