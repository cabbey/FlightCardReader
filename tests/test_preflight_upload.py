"""Tests for preflight image upload and clear-lost endpoints."""

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import JSON, Column, Integer, String
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from flight_card_scanner.services.image_service import (
    get_preflight_image_path,
    save_preflight_image,
)


# ---------------------------------------------------------------------------
# Unit tests for image_service preflight functions
# ---------------------------------------------------------------------------


class TestGetPreflightImagePath:
    """Tests for get_preflight_image_path."""

    def test_inserts_preflight_suffix(self):
        assert get_preflight_image_path("abc123.jpg") == "abc123-preflight.jpg"

    def test_handles_png_extension(self):
        assert get_preflight_image_path("uuid-test.png") == "uuid-test-preflight.png"

    def test_handles_no_extension(self):
        assert get_preflight_image_path("noext") == "noext-preflight"

    def test_handles_multiple_dots(self):
        # Only the last dot separates extension
        assert get_preflight_image_path("a.b.c.jpg") == "a.b.c-preflight.jpg"


class TestSavePreflightImage:
    """Tests for save_preflight_image."""

    def test_saves_image_with_preflight_suffix(self, tmp_path: Path):
        content = b"\x89PNG" + b"\x00" * 50
        result = save_preflight_image("test-uuid.png", content, tmp_path)
        assert result == "test-uuid-preflight.png"
        assert (tmp_path / result).read_bytes() == content

    def test_raises_if_dir_does_not_exist(self, tmp_path: Path):
        from flight_card_scanner.exceptions import ImageStorageError

        with pytest.raises(ImageStorageError, match="does not exist"):
            save_preflight_image("x.jpg", b"data", tmp_path / "nonexistent")

    def test_raises_if_path_is_not_directory(self, tmp_path: Path):
        from flight_card_scanner.exceptions import ImageStorageError

        file_path = tmp_path / "afile.txt"
        file_path.write_text("not a dir")
        with pytest.raises(ImageStorageError, match="not a directory"):
            save_preflight_image("x.jpg", b"data", file_path)


# ---------------------------------------------------------------------------
# Integration tests for preflight upload endpoint
# ---------------------------------------------------------------------------

# Shared ORM base for test models
class _TestBase(DeclarativeBase):
    pass


class _FakeFlightRecord(_TestBase):
    __tablename__ = "flight_records"
    id = Column(Integer, primary_key=True)
    image_path = Column(String(512), nullable=False)
    overflow = Column(JSON, nullable=True)


@dataclass
class _FakeEventConfig:
    image_store_path: Path


@dataclass
class _FakeEventInfo:
    event_config: _FakeEventConfig
    session_factory: async_sessionmaker


def _make_user(email="flyer@test.com", role="flyer"):
    """Create a mock user object."""
    user = MagicMock()
    user.email = email
    user.role = role
    user.active = True
    return user


async def _create_test_app(tmp_path: Path, user=None, record_overflow=None):
    """Create a minimal FastAPI app with preflight endpoints for testing."""
    from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
    from starlette.middleware.base import BaseHTTPMiddleware

    from flight_card_scanner.dependencies.auth import Role, require_role

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # Create tables and seed data
    async with engine.begin() as conn:
        await conn.run_sync(_TestBase.metadata.create_all)
    async with session_factory() as session:
        record = _FakeFlightRecord(
            id=1,
            image_path="test-uuid.jpg",
            overflow=record_overflow or {},
        )
        session.add(record)
        await session.commit()

    event_info = _FakeEventInfo(
        event_config=_FakeEventConfig(image_store_path=tmp_path),
        session_factory=session_factory,
    )

    app = FastAPI()

    # Middleware to set user on request.state
    class FakeAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.user = user
            return await call_next(request)

    app.add_middleware(FakeAuthMiddleware)

    # Create event router
    event_router = APIRouter(prefix="/events/{event_path:path}")

    @event_router.post(
        "/api/record/{record_id}/preflight",
        dependencies=[Depends(require_role(Role.FLYER))],
    )
    async def upload_preflight(
        request: Request,
        event_path: str,
        record_id: int,
    ):
        from sqlalchemy import select as sa_select
        from sqlalchemy.orm.attributes import flag_modified

        from flight_card_scanner.services.image_service import (
            get_preflight_image_path,
            save_preflight_image,
        )

        form = await request.form()
        preflight_image = form.get("preflight_image")
        mark_lost_raw = form.get("mark_lost", "false")
        mark_lost = mark_lost_raw in ("true", "1", "on", "True")

        if preflight_image is None or not hasattr(preflight_image, "read"):
            raise HTTPException(status_code=400, detail="No preflight image provided")

        content_type = getattr(preflight_image, "content_type", "") or ""
        if content_type not in ("image/jpeg", "image/png"):
            raise HTTPException(
                status_code=400,
                detail="Invalid file type. Only JPEG and PNG are accepted.",
            )

        file_bytes = await preflight_image.read()
        if not file_bytes:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")

        async with session_factory() as db:
            result = await db.execute(
                sa_select(_FakeFlightRecord).where(_FakeFlightRecord.id == record_id)
            )
            record = result.scalar_one_or_none()
            if record is None:
                raise HTTPException(status_code=404, detail="Record not found")

            preflight_filename = get_preflight_image_path(record.image_path)
            store_path = event_info.event_config.image_store_path
            if (store_path / preflight_filename).exists():
                raise HTTPException(
                    status_code=409,
                    detail="Preflight image already exists for this record",
                )

            save_preflight_image(record.image_path, file_bytes, store_path)

            overflow = dict(record.overflow) if record.overflow else {}
            overflow["preflight_status"] = "pending"
            current_user = getattr(request.state, "user", None)
            overflow["preflight_uploaded_by"] = (
                current_user.email if current_user else "unknown"
            )
            if mark_lost:
                overflow["is_lost"] = True

            record.overflow = overflow
            flag_modified(record, "overflow")
            await db.commit()

        return {"message": "Preflight image uploaded successfully", "status": "pending"}

    @event_router.post(
        "/api/record/{record_id}/clear-lost",
        dependencies=[Depends(require_role(Role.FLYER))],
    )
    async def clear_lost(
        request: Request,
        event_path: str,
        record_id: int,
    ):
        from sqlalchemy import select as sa_select
        from sqlalchemy.orm.attributes import flag_modified

        async with session_factory() as db:
            result = await db.execute(
                sa_select(_FakeFlightRecord).where(_FakeFlightRecord.id == record_id)
            )
            record = result.scalar_one_or_none()
            if record is None:
                raise HTTPException(status_code=404, detail="Record not found")

            overflow = dict(record.overflow) if record.overflow else {}
            if not overflow.get("is_lost"):
                raise HTTPException(
                    status_code=400, detail="Record is not marked as lost"
                )

            current_user = getattr(request.state, "user", None)
            uploaded_by = overflow.get("preflight_uploaded_by", "")
            user_role = current_user.role if current_user else ""
            user_email = current_user.email if current_user else ""

            if user_role not in ("admin", "data_entry") and user_email != uploaded_by:
                raise HTTPException(
                    status_code=403,
                    detail="Only the user who marked it lost or admin/data_entry can clear this",
                )

            overflow["is_lost"] = False
            record.overflow = overflow
            flag_modified(record, "overflow")
            await db.commit()

        return {"message": "Lost status cleared"}

    app.include_router(event_router)
    return app


@pytest.mark.asyncio
async def test_preflight_upload_success(tmp_path: Path):
    """Authenticated flyer can upload a preflight image."""
    user = _make_user()
    app = await _create_test_app(tmp_path, user=user)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/events/test-event/api/record/1/preflight",
            files={
                "preflight_image": (
                    "photo.jpg",
                    b"\xff\xd8\xff" + b"\x00" * 50,
                    "image/jpeg",
                )
            },
            data={"mark_lost": "false"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "pending"
    assert (tmp_path / "test-uuid-preflight.jpg").exists()


@pytest.mark.asyncio
async def test_preflight_upload_sets_overflow(tmp_path: Path):
    """Upload sets preflight_status and preflight_uploaded_by in overflow."""
    user = _make_user(email="pilot@example.com")
    app = await _create_test_app(tmp_path, user=user)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/events/test-event/api/record/1/preflight",
            files={
                "preflight_image": (
                    "img.png",
                    b"\x89PNG" + b"\x00" * 50,
                    "image/png",
                )
            },
            data={"mark_lost": "false"},
        )

    assert response.status_code == 200
    # Verify the image file was saved
    assert (tmp_path / "test-uuid-preflight.jpg").exists()


@pytest.mark.asyncio
async def test_preflight_upload_mark_lost(tmp_path: Path):
    """Upload with mark_lost=true sets overflow.is_lost=True."""
    user = _make_user()
    app = await _create_test_app(tmp_path, user=user)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/events/test-event/api/record/1/preflight",
            files={
                "preflight_image": (
                    "photo.jpg",
                    b"\xff\xd8\xff" + b"\x00" * 50,
                    "image/jpeg",
                )
            },
            data={"mark_lost": "true"},
        )

    assert response.status_code == 200
    assert (tmp_path / "test-uuid-preflight.jpg").exists()


@pytest.mark.asyncio
async def test_preflight_upload_rejects_invalid_type(tmp_path: Path):
    """Upload rejects non-JPEG/PNG file types."""
    user = _make_user()
    app = await _create_test_app(tmp_path, user=user)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/events/test-event/api/record/1/preflight",
            files={
                "preflight_image": (
                    "doc.pdf",
                    b"%PDF-1.4",
                    "application/pdf",
                )
            },
            data={"mark_lost": "false"},
        )

    assert response.status_code == 400
    assert "Invalid file type" in response.json()["detail"]


@pytest.mark.asyncio
async def test_preflight_upload_unauthenticated(tmp_path: Path):
    """Unauthenticated users cannot upload preflight images."""
    app = await _create_test_app(tmp_path, user=None)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/events/test-event/api/record/1/preflight",
            files={
                "preflight_image": (
                    "photo.jpg",
                    b"\xff\xd8\xff" + b"\x00" * 50,
                    "image/jpeg",
                )
            },
            data={"mark_lost": "false"},
            headers={"Accept": "application/json"},
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_clear_lost_by_uploader(tmp_path: Path):
    """The user who set lost status can clear it."""
    user = _make_user(email="flyer@test.com", role="flyer")
    overflow = {
        "is_lost": True,
        "preflight_status": "pending",
        "preflight_uploaded_by": "flyer@test.com",
    }
    app = await _create_test_app(tmp_path, user=user, record_overflow=overflow)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/events/test-event/api/record/1/clear-lost",
        )

    assert response.status_code == 200
    assert response.json()["message"] == "Lost status cleared"


@pytest.mark.asyncio
async def test_clear_lost_by_admin(tmp_path: Path):
    """Admin can clear lost status regardless of who set it."""
    user = _make_user(email="admin@test.com", role="admin")
    overflow = {
        "is_lost": True,
        "preflight_status": "pending",
        "preflight_uploaded_by": "flyer@test.com",
    }
    app = await _create_test_app(tmp_path, user=user, record_overflow=overflow)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/events/test-event/api/record/1/clear-lost",
        )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_clear_lost_forbidden_for_other_flyer(tmp_path: Path):
    """A different flyer cannot clear lost status they did not set."""
    user = _make_user(email="other@test.com", role="flyer")
    overflow = {
        "is_lost": True,
        "preflight_status": "pending",
        "preflight_uploaded_by": "flyer@test.com",
    }
    app = await _create_test_app(tmp_path, user=user, record_overflow=overflow)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/events/test-event/api/record/1/clear-lost",
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_preflight_upload_duplicate_rejected(tmp_path: Path):
    """Cannot upload a second preflight image if one already exists."""
    user = _make_user()
    # Create the preflight file first
    (tmp_path / "test-uuid-preflight.jpg").write_bytes(b"existing")

    app = await _create_test_app(tmp_path, user=user)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/events/test-event/api/record/1/preflight",
            files={
                "preflight_image": (
                    "photo.jpg",
                    b"\xff\xd8\xff" + b"\x00" * 50,
                    "image/jpeg",
                )
            },
            data={"mark_lost": "false"},
        )

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]
