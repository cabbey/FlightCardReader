"""Unit tests for the admin API router endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from flight_card_scanner.database import get_db
from flight_card_scanner.routers import admin
from flight_card_scanner.routers.admin import router
from flight_card_scanner.services.extraction_service import ExtractionMode


@pytest.fixture
def mock_db():
    """Create a mock async database session."""
    return AsyncMock()


@pytest.fixture
def app(mock_db):
    """Create a FastAPI test app with the admin router included."""
    app = FastAPI()
    app.include_router(router)

    # Override the get_db dependency so we don't need a real database
    async def override_get_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_get_db
    return app


@pytest.fixture
def mock_extraction_service():
    """Create a mock ExtractionService."""
    svc = AsyncMock()
    svc.mode = ExtractionMode.IMMEDIATE
    svc.set_mode = AsyncMock()
    svc.trigger_pending = AsyncMock(return_value=3)
    svc.enqueue = AsyncMock()
    return svc


@pytest.fixture(autouse=True)
def configure_admin(mock_extraction_service):
    """Wire up the mock extraction service for the admin router."""
    admin.configure(mock_extraction_service)
    yield
    # Reset module state
    admin._extraction_service = None


@pytest.fixture
async def client(app):
    """Async HTTP client for testing."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# POST /api/admin/mode
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_set_mode_immediate(client, mock_extraction_service):
    """POST /admin/mode with immediate sets mode and returns confirmation."""
    response = await client.post("/api/admin/mode", json={"mode": "immediate"})
    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "immediate"
    assert "immediate" in data["message"].lower()
    mock_extraction_service.set_mode.assert_awaited_once_with(ExtractionMode.IMMEDIATE)


@pytest.mark.anyio
async def test_set_mode_deferred(client, mock_extraction_service):
    """POST /admin/mode with deferred sets mode and returns confirmation."""
    response = await client.post("/api/admin/mode", json={"mode": "deferred"})
    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "deferred"
    mock_extraction_service.set_mode.assert_awaited_once_with(ExtractionMode.DEFERRED)


@pytest.mark.anyio
async def test_set_mode_invalid(client):
    """POST /admin/mode with invalid mode returns 422."""
    response = await client.post("/api/admin/mode", json={"mode": "invalid_mode"})
    assert response.status_code == 422
    assert "invalid mode" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# POST /api/admin/trigger
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_trigger_extraction(client, mock_extraction_service):
    """POST /admin/trigger dispatches pending records."""
    response = await client.post("/api/admin/trigger")
    assert response.status_code == 200
    data = response.json()
    assert data["dispatched"] == 3
    mock_extraction_service.trigger_pending.assert_awaited_once()


# ---------------------------------------------------------------------------
# POST /api/admin/requeue
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_requeue_all_failed(client, mock_extraction_service):
    """POST /admin/requeue resets all failed records and enqueues them."""
    # Create mock records
    record1 = MagicMock()
    record1.id = 1
    record2 = MagicMock()
    record2.id = 2

    with patch(
        "flight_card_scanner.routers.admin.record_service.get_by_status",
        new_callable=AsyncMock,
        return_value=[record1, record2],
    ), patch(
        "flight_card_scanner.routers.admin.record_service.set_status",
        new_callable=AsyncMock,
    ) as mock_set_status:
        response = await client.post("/api/admin/requeue")

    assert response.status_code == 200
    data = response.json()
    assert data["requeued"] == 2
    assert mock_set_status.await_count == 2
    assert mock_extraction_service.enqueue.await_count == 2


@pytest.mark.anyio
async def test_requeue_all_failed_empty(client, mock_extraction_service):
    """POST /admin/requeue with no failed records returns 0."""
    with patch(
        "flight_card_scanner.routers.admin.record_service.get_by_status",
        new_callable=AsyncMock,
        return_value=[],
    ):
        response = await client.post("/api/admin/requeue")

    assert response.status_code == 200
    assert response.json()["requeued"] == 0


# ---------------------------------------------------------------------------
# POST /api/admin/requeue/{record_id}
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_requeue_single_success(client, mock_extraction_service):
    """POST /admin/requeue/1 resets a failed record."""
    record = MagicMock()
    record.id = 1
    record.extraction_status = "extraction_failed"

    with patch(
        "flight_card_scanner.routers.admin.record_service.get",
        new_callable=AsyncMock,
        return_value=record,
    ), patch(
        "flight_card_scanner.routers.admin.record_service.set_status",
        new_callable=AsyncMock,
    ) as mock_set_status:
        response = await client.post("/api/admin/requeue/1")

    assert response.status_code == 200
    assert response.json()["requeued"] == 1
    mock_set_status.assert_awaited_once()
    mock_extraction_service.enqueue.assert_awaited_once_with(1)


@pytest.mark.anyio
async def test_requeue_single_not_found(client):
    """POST /admin/requeue/999 returns 404 when record doesn't exist."""
    with patch(
        "flight_card_scanner.routers.admin.record_service.get",
        new_callable=AsyncMock,
        return_value=None,
    ):
        response = await client.post("/api/admin/requeue/999")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_requeue_single_wrong_status(client):
    """POST /admin/requeue/1 returns 422 when record is not extraction_failed."""
    record = MagicMock()
    record.id = 1
    record.extraction_status = "extracted"

    with patch(
        "flight_card_scanner.routers.admin.record_service.get",
        new_callable=AsyncMock,
        return_value=record,
    ):
        response = await client.post("/api/admin/requeue/1")

    assert response.status_code == 422
    assert "extracted" in response.json()["detail"]
