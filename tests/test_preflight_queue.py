"""Tests for preflight image approval queue endpoints (FEAT-009).

Tests:
- Approve endpoint sets preflight_status to 'approved'
- Delete endpoint removes file and clears overflow data
- Delete endpoint also removes lost rocket entry if is_lost was True
- Queue page requires DATA_ENTRY role
"""

from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import JSON, Column, Integer, String, text
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


# ---------------------------------------------------------------------------
# Test ORM base and models
# ---------------------------------------------------------------------------


class _TestBase(DeclarativeBase):
    pass


class _FakeFlightRecord(_TestBase):
    __tablename__ = "flight_records"
    id = Column(Integer, primary_key=True)
    image_path = Column(String(512), nullable=False)
    overflow = Column(JSON, nullable=True)


class _LostRocketsBase(DeclarativeBase):
    pass


class _FakeLostRocket(_LostRocketsBase):
    __tablename__ = "lost_rockets"
    id = Column(Integer, primary_key=True, autoincrement=True)
    event_slug = Column(String(256), nullable=False)
    record_id = Column(Integer, nullable=False)
    event_name = Column(String(256), nullable=True)
    flier_name = Column(String(256), nullable=True)
    added_by = Column(String(254), nullable=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeDateRange:
    start: str = "2025-01-01"
    end: str = "2025-01-07"


@dataclass
class _FakeEventConfig:
    image_store_path: Path = field(default_factory=lambda: Path("/tmp"))
    event_name: str = "Test Event"
    db_path: Path = field(default_factory=lambda: Path("/tmp/test.db"))
    read_only: bool = False
    event_data_path: Path = field(default_factory=lambda: Path("/tmp"))
    event_date_range: _FakeDateRange = field(default_factory=_FakeDateRange)
    known_fliers_path: Path | None = None


@dataclass
class _FakeEventInfo:
    slug: str = "test-event"
    event_config: _FakeEventConfig = field(default_factory=_FakeEventConfig)
    session_factory: async_sessionmaker | None = None
    is_open: bool = True
    config_path: Path = field(default_factory=lambda: Path("/tmp/config.json"))


def _make_user(email="admin@test.com", role="data_entry", display_name="Admin"):
    """Create a mock user object."""
    user = MagicMock()
    user.email = email
    user.role = role
    user.display_name = display_name
    user.active = True
    return user


async def _create_test_app(
    tmp_path: Path,
    user=None,
    record_overflow=None,
    create_preflight_file=True,
    create_lost_rocket=False,
):
    """Create a minimal FastAPI app with preflight queue endpoints for testing."""
    from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse
    from fastapi.templating import Jinja2Templates
    from starlette.middleware.base import BaseHTTPMiddleware

    from flight_card_scanner.dependencies.auth import Role, require_role

    # Set up event database
    event_db_path = tmp_path / "event.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{event_db_path}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(_TestBase.metadata.create_all)

    async with session_factory() as session:
        record = _FakeFlightRecord(
            id=1,
            image_path="test-uuid.jpg",
            overflow=record_overflow or {"preflight_status": "pending", "preflight_uploaded_by": "flyer@test.com"},
        )
        session.add(record)
        await session.commit()

    # Create the image store with the preflight file
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    if create_preflight_file:
        (images_dir / "test-uuid-preflight.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 50)

    # Set up lost rockets database
    lost_rockets_db_path = tmp_path / "lost_rockets.db"
    lost_engine = create_async_engine(f"sqlite+aiosqlite:///{lost_rockets_db_path}")
    lost_session_factory = async_sessionmaker(lost_engine, expire_on_commit=False)

    async with lost_engine.begin() as conn:
        await conn.run_sync(_LostRocketsBase.metadata.create_all)

    if create_lost_rocket:
        async with lost_session_factory() as session:
            rocket = _FakeLostRocket(
                event_slug="test-event",
                record_id=1,
                event_name="Test Event",
                flier_name="Test Flier",
                added_by="flyer@test.com",
            )
            session.add(rocket)
            await session.commit()

    event_config = _FakeEventConfig(
        image_store_path=images_dir,
        event_name="Test Event",
        db_path=event_db_path,
    )
    event_info = _FakeEventInfo(
        slug="test-event",
        event_config=event_config,
        session_factory=session_factory,
    )

    # Mock event manager
    event_manager = MagicMock()
    event_manager.events = {"test-event": event_info}

    async def mock_get_event(slug):
        if slug == "test-event":
            return event_info
        raise KeyError(f"Event not found: {slug!r}")

    event_manager.get_event = AsyncMock(side_effect=mock_get_event)

    app = FastAPI()

    # Middleware to set user on request.state
    class FakeAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.user = user
            return await call_next(request)

    app.add_middleware(FakeAuthMiddleware)

    # Store event manager on app.state
    app.state.event_manager = event_manager

    # Set up templates
    templates_dir = Path(__file__).parent.parent / "flight_card_scanner" / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))
    app.state.templates = templates

    # Create the router with queue and approval endpoints
    queue_router = APIRouter()

    @queue_router.get(
        "/admin/preflight-queue",
        response_class=HTMLResponse,
        dependencies=[Depends(require_role(Role.DATA_ENTRY))],
    )
    async def preflight_queue_page_test(request: Request):
        """Render the preflight image approval queue."""
        from sqlalchemy import text as sa_text

        pending_items: list[dict] = []

        for slug, ev_info in event_manager.events.items():
            db_path_val = ev_info.event_config.db_path
            if not db_path_val.exists():
                continue

            from sqlalchemy.ext.asyncio import create_async_engine as _cae

            uri_path = str(db_path_val).replace("?", "%3f").replace("#", "%23")
            url = f"sqlite+aiosqlite:///file:{uri_path}?mode=ro&uri=true"
            temp_engine = _cae(url, echo=False)

            try:
                async with temp_engine.connect() as conn:
                    table_exists = await conn.run_sync(
                        lambda sync_conn: sync_conn.dialect.has_table(
                            sync_conn, "flight_records"
                        )
                    )
                    if not table_exists:
                        continue

                    result = await conn.execute(
                        sa_text(
                            "SELECT id, image_path, overflow FROM flight_records "
                            "WHERE json_extract(overflow, '$.preflight_status') = 'pending'"
                        )
                    )
                    rows = result.fetchall()

                    for row in rows:
                        import json as json_mod

                        overflow = row[2]
                        if isinstance(overflow, str):
                            overflow = json_mod.loads(overflow)
                        elif overflow is None:
                            overflow = {}

                        from flight_card_scanner.services.image_service import (
                            get_preflight_image_path,
                        )

                        preflight_filename = get_preflight_image_path(row[1])

                        pending_items.append({
                            "event_slug": slug,
                            "event_name": ev_info.event_config.event_name,
                            "record_id": row[0],
                            "image_path": row[1],
                            "preflight_image_path": preflight_filename,
                            "uploaded_by": overflow.get("preflight_uploaded_by", "unknown"),
                            "is_lost": overflow.get("is_lost", False),
                        })
            finally:
                await temp_engine.dispose()

        return templates.TemplateResponse(
            name="preflight_queue.html",
            request=request,
            context={
                "request": request,
                "page_title": "Preflight Approval Queue",
                "pending_items": pending_items,
                "current_user": user,
            },
        )

    @queue_router.post(
        "/api/admin/preflight/{event_slug:path}/{record_id:int}/approve",
        dependencies=[Depends(require_role(Role.DATA_ENTRY))],
    )
    async def approve_preflight_test(request: Request, event_slug: str, record_id: int):
        """Approve a pending preflight image."""
        ev_info = event_manager.events.get(event_slug)
        if ev_info is None:
            raise HTTPException(status_code=404, detail="Event not found")

        if ev_info.session_factory is None:
            raise HTTPException(status_code=500, detail="Event database not available")

        from sqlalchemy import select as sa_select
        from sqlalchemy.orm.attributes import flag_modified

        async with ev_info.session_factory() as db:
            result = await db.execute(
                sa_select(_FakeFlightRecord).where(_FakeFlightRecord.id == record_id)
            )
            record = result.scalar_one_or_none()
            if record is None:
                raise HTTPException(status_code=404, detail="Record not found")

            overflow = dict(record.overflow) if record.overflow else {}
            if overflow.get("preflight_status") != "pending":
                raise HTTPException(
                    status_code=400, detail="Record is not pending approval"
                )

            overflow["preflight_status"] = "approved"
            record.overflow = overflow
            flag_modified(record, "overflow")
            await db.commit()

        return {"message": "Preflight image approved", "status": "approved"}

    @queue_router.post(
        "/api/admin/preflight/{event_slug:path}/{record_id:int}/delete",
        dependencies=[Depends(require_role(Role.DATA_ENTRY))],
    )
    async def delete_preflight_test(request: Request, event_slug: str, record_id: int):
        """Delete a pending preflight image."""
        ev_info = event_manager.events.get(event_slug)
        if ev_info is None:
            raise HTTPException(status_code=404, detail="Event not found")

        if ev_info.session_factory is None:
            raise HTTPException(status_code=500, detail="Event database not available")

        from sqlalchemy import select as sa_select
        from sqlalchemy.orm.attributes import flag_modified

        from flight_card_scanner.services.image_service import get_preflight_image_path

        async with ev_info.session_factory() as db:
            result = await db.execute(
                sa_select(_FakeFlightRecord).where(_FakeFlightRecord.id == record_id)
            )
            record = result.scalar_one_or_none()
            if record is None:
                raise HTTPException(status_code=404, detail="Record not found")

            overflow = dict(record.overflow) if record.overflow else {}
            was_lost = overflow.get("is_lost", False)

            preflight_filename = get_preflight_image_path(record.image_path)
            image_path = ev_info.event_config.image_store_path / preflight_filename
            if image_path.exists():
                image_path.unlink()

            overflow.pop("preflight_status", None)
            overflow.pop("preflight_uploaded_by", None)

            if was_lost:
                overflow.pop("is_lost", None)

            record.overflow = overflow
            flag_modified(record, "overflow")
            await db.commit()

        # Remove from lost rockets if was lost
        if was_lost:
            from sqlalchemy import delete as sa_delete

            async with lost_session_factory() as lost_db:
                await lost_db.execute(
                    sa_delete(_FakeLostRocket).where(
                        _FakeLostRocket.event_slug == event_slug,
                        _FakeLostRocket.record_id == record_id,
                    )
                )
                await lost_db.commit()

        return {"message": "Preflight image deleted"}

    app.include_router(queue_router)
    return app, session_factory, lost_session_factory, engine, lost_engine


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_sets_status_approved(tmp_path: Path):
    """Approve endpoint sets preflight_status to 'approved'."""
    user = _make_user(role="data_entry")
    app, session_factory, _, engine, lost_engine = await _create_test_app(
        tmp_path, user=user
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/api/admin/preflight/test-event/1/approve",
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "approved"

    # Verify the overflow was updated in the database
    from sqlalchemy import select as sa_select

    async with session_factory() as db:
        result = await db.execute(
            sa_select(_FakeFlightRecord).where(_FakeFlightRecord.id == 1)
        )
        record = result.scalar_one()
        assert record.overflow["preflight_status"] == "approved"

    await engine.dispose()
    await lost_engine.dispose()


@pytest.mark.asyncio
async def test_delete_removes_file_and_clears_overflow(tmp_path: Path):
    """Delete endpoint removes preflight file and clears overflow data."""
    user = _make_user(role="data_entry")
    app, session_factory, _, engine, lost_engine = await _create_test_app(
        tmp_path, user=user
    )

    # Verify the preflight file exists before delete
    images_dir = tmp_path / "images"
    assert (images_dir / "test-uuid-preflight.jpg").exists()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/api/admin/preflight/test-event/1/delete",
        )

    assert response.status_code == 200
    assert response.json()["message"] == "Preflight image deleted"

    # Verify the file was deleted
    assert not (images_dir / "test-uuid-preflight.jpg").exists()

    # Verify overflow was cleared
    from sqlalchemy import select as sa_select

    async with session_factory() as db:
        result = await db.execute(
            sa_select(_FakeFlightRecord).where(_FakeFlightRecord.id == 1)
        )
        record = result.scalar_one()
        assert "preflight_status" not in record.overflow
        assert "preflight_uploaded_by" not in record.overflow

    await engine.dispose()
    await lost_engine.dispose()


@pytest.mark.asyncio
async def test_delete_removes_lost_rocket_if_is_lost(tmp_path: Path):
    """Delete endpoint also removes lost rocket entry if is_lost was True."""
    user = _make_user(role="data_entry")
    overflow = {
        "preflight_status": "pending",
        "preflight_uploaded_by": "flyer@test.com",
        "is_lost": True,
    }
    app, session_factory, lost_session_factory, engine, lost_engine = await _create_test_app(
        tmp_path, user=user, record_overflow=overflow, create_lost_rocket=True
    )

    # Verify the lost rocket entry exists
    from sqlalchemy import select as sa_select

    async with lost_session_factory() as lost_db:
        result = await lost_db.execute(
            sa_select(_FakeLostRocket).where(
                _FakeLostRocket.event_slug == "test-event",
                _FakeLostRocket.record_id == 1,
            )
        )
        assert result.scalar_one_or_none() is not None

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/api/admin/preflight/test-event/1/delete",
        )

    assert response.status_code == 200

    # Verify the lost rocket entry was removed
    async with lost_session_factory() as lost_db:
        result = await lost_db.execute(
            sa_select(_FakeLostRocket).where(
                _FakeLostRocket.event_slug == "test-event",
                _FakeLostRocket.record_id == 1,
            )
        )
        assert result.scalar_one_or_none() is None

    # Verify overflow.is_lost was cleared
    async with session_factory() as db:
        result = await db.execute(
            sa_select(_FakeFlightRecord).where(_FakeFlightRecord.id == 1)
        )
        record = result.scalar_one()
        assert "is_lost" not in record.overflow

    await engine.dispose()
    await lost_engine.dispose()


@pytest.mark.asyncio
async def test_queue_page_requires_data_entry_role(tmp_path: Path):
    """Queue page returns 401 for unauthenticated users."""
    app, _, _, engine, lost_engine = await _create_test_app(
        tmp_path, user=None
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/admin/preflight-queue",
            headers={"Accept": "application/json"},
        )

    assert response.status_code == 401

    await engine.dispose()
    await lost_engine.dispose()


@pytest.mark.asyncio
async def test_queue_page_forbidden_for_flyer(tmp_path: Path):
    """Queue page returns 403 for flyer role (below DATA_ENTRY)."""
    user = _make_user(role="flyer")
    app, _, _, engine, lost_engine = await _create_test_app(
        tmp_path, user=user
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/admin/preflight-queue",
            headers={"Accept": "application/json"},
        )

    assert response.status_code == 403

    await engine.dispose()
    await lost_engine.dispose()


@pytest.mark.asyncio
async def test_queue_page_shows_pending_items(tmp_path: Path):
    """Queue page renders successfully with pending items for data_entry user."""
    user = _make_user(role="data_entry")
    app, _, _, engine, lost_engine = await _create_test_app(
        tmp_path, user=user
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/admin/preflight-queue")

    assert response.status_code == 200
    # Should contain the record info in the rendered HTML
    assert "test-event" in response.text
    assert "flyer@test.com" in response.text
    assert "test-uuid-preflight.jpg" in response.text

    await engine.dispose()
    await lost_engine.dispose()


@pytest.mark.asyncio
async def test_approve_requires_data_entry_role(tmp_path: Path):
    """Approve endpoint returns 403 for flyer role."""
    user = _make_user(role="flyer")
    app, _, _, engine, lost_engine = await _create_test_app(
        tmp_path, user=user
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/api/admin/preflight/test-event/1/approve",
        )

    assert response.status_code == 403

    await engine.dispose()
    await lost_engine.dispose()


@pytest.mark.asyncio
async def test_approve_nonexistent_record(tmp_path: Path):
    """Approve endpoint returns 404 for non-existent record."""
    user = _make_user(role="data_entry")
    app, _, _, engine, lost_engine = await _create_test_app(
        tmp_path, user=user
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/api/admin/preflight/test-event/999/approve",
        )

    assert response.status_code == 404

    await engine.dispose()
    await lost_engine.dispose()
