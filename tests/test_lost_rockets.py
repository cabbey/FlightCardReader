"""Tests for the lost rockets database and endpoints.

Verifies:
- LostRocket model creation and unique constraints
- Creating a lost rocket entry via preflight upload
- Clearing lost removes the DB row
- GET /lost-rockets returns 200 and shows entries
- Lost rocket entry has correct data from the flight record
"""

from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from flight_card_scanner.lost_rockets_database import (
    create_lost_rockets_tables,
    init_lost_rockets_engine,
)
from flight_card_scanner.lost_rockets_models import LostRocket, LostRocketsBase


# ---------------------------------------------------------------------------
# Unit tests for model and database
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_lost_rockets_tables(tmp_path: Path):
    """Tables are created without error."""
    db_path = tmp_path / "lost_rockets.db"
    engine = init_lost_rockets_engine(db_path)
    await create_lost_rockets_tables(engine)
    # Engine should work
    async with engine.begin() as conn:
        result = await conn.run_sync(
            lambda sync_conn: sync_conn.execute(
                LostRocketsBase.metadata.tables["lost_rockets"].select()
            ).fetchall()
        )
    assert result == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_insert_lost_rocket(tmp_path: Path):
    """Can insert a LostRocket entry and retrieve it."""
    db_path = tmp_path / "lost_rockets.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(LostRocketsBase.metadata.create_all)

    async with session_factory() as session:
        entry = LostRocket(
            event_slug="2026/nxrs",
            event_name="NXRS Spring Launch 2026",
            record_id=42,
            flier_name="Jane Doe",
            rocket_colors=["red", "white"],
            diameter="4 in",
            length="36 in",
            motor_designation="H128W",
            flight_date=date(2026, 4, 25),
            preflight_image_path="abc123-preflight.jpg",
            added_by="flyer@example.com",
        )
        session.add(entry)
        await session.commit()

    async with session_factory() as session:
        result = await session.execute(select(LostRocket))
        rows = result.scalars().all()
        assert len(rows) == 1
        assert rows[0].flier_name == "Jane Doe"
        assert rows[0].rocket_colors == ["red", "white"]
        assert rows[0].event_slug == "2026/nxrs"
        assert rows[0].record_id == 42
        assert rows[0].motor_designation == "H128W"

    await engine.dispose()


@pytest.mark.asyncio
async def test_unique_constraint(tmp_path: Path):
    """Duplicate (event_slug, record_id) raises IntegrityError."""
    from sqlalchemy.exc import IntegrityError

    db_path = tmp_path / "lost_rockets.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(LostRocketsBase.metadata.create_all)

    async with session_factory() as session:
        entry1 = LostRocket(
            event_slug="2026/nxrs",
            record_id=1,
            added_by="user@test.com",
        )
        session.add(entry1)
        await session.commit()

    with pytest.raises(IntegrityError):
        async with session_factory() as session:
            entry2 = LostRocket(
                event_slug="2026/nxrs",
                record_id=1,
                added_by="user2@test.com",
            )
            session.add(entry2)
            await session.commit()

    await engine.dispose()


@pytest.mark.asyncio
async def test_delete_lost_rocket(tmp_path: Path):
    """Deleting by event_slug and record_id removes the row."""
    from sqlalchemy import delete as sa_delete

    db_path = tmp_path / "lost_rockets.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(LostRocketsBase.metadata.create_all)

    # Insert an entry
    async with session_factory() as session:
        entry = LostRocket(
            event_slug="2026/nxrs",
            record_id=5,
            flier_name="Bob",
            added_by="bob@test.com",
        )
        session.add(entry)
        await session.commit()

    # Delete it
    async with session_factory() as session:
        await session.execute(
            sa_delete(LostRocket).where(
                LostRocket.event_slug == "2026/nxrs",
                LostRocket.record_id == 5,
            )
        )
        await session.commit()

    # Verify gone
    async with session_factory() as session:
        result = await session.execute(select(LostRocket))
        assert result.scalars().all() == []

    await engine.dispose()


# ---------------------------------------------------------------------------
# Integration tests for lost rockets page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lost_rockets_page_empty(tmp_path: Path):
    """GET /lost-rockets returns 200 with empty state when no rockets are lost."""
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates
    from starlette.middleware.base import BaseHTTPMiddleware
    from fastapi import Request

    from flight_card_scanner.routers import lost_rockets as lost_rockets_mod
    from flight_card_scanner.lost_rockets_database import (
        _lost_rockets_session,
    )
    import flight_card_scanner.lost_rockets_database as lr_db_mod

    templates_dir = Path(__file__).resolve().parent.parent / "flight_card_scanner" / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))

    # Set up a test lost rockets DB
    db_path = tmp_path / "lost_rockets.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(LostRocketsBase.metadata.create_all)

    # Patch the module-level session factory
    original_session = lr_db_mod._lost_rockets_session
    lr_db_mod._lost_rockets_session = session_factory

    try:
        app = FastAPI()

        class FakeAuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                request.state.user = None
                request.state.session_token = None
                request.state.clear_session_cookie = False
                return await call_next(request)

        app.add_middleware(FakeAuthMiddleware)
        lost_rockets_mod.configure(templates=templates)
        app.include_router(lost_rockets_mod.router)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/lost-rockets")

        assert response.status_code == 200
        assert "Lost Rockets" in response.text
        assert "No lost rockets" in response.text
    finally:
        lr_db_mod._lost_rockets_session = original_session
        await engine.dispose()


@pytest.mark.asyncio
async def test_lost_rockets_page_shows_entries(tmp_path: Path):
    """GET /lost-rockets shows rocket entries when they exist."""
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates
    from starlette.middleware.base import BaseHTTPMiddleware
    from fastapi import Request

    from flight_card_scanner.routers import lost_rockets as lost_rockets_mod
    import flight_card_scanner.lost_rockets_database as lr_db_mod

    templates_dir = Path(__file__).resolve().parent.parent / "flight_card_scanner" / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))

    # Set up a test lost rockets DB
    db_path = tmp_path / "lost_rockets.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(LostRocketsBase.metadata.create_all)

    # Insert a test entry
    async with session_factory() as session:
        entry = LostRocket(
            event_slug="2026/nxrs",
            event_name="NXRS Spring Launch 2026",
            record_id=10,
            flier_name="Alice Rocketeer",
            rocket_colors=["blue", "silver"],
            diameter="3 in",
            length="24 in",
            motor_designation="G80T",
            flight_date=date(2026, 4, 26),
            preflight_image_path="uuid123-preflight.jpg",
            added_by="alice@test.com",
        )
        session.add(entry)
        await session.commit()

    # Patch the module-level session factory
    original_session = lr_db_mod._lost_rockets_session
    lr_db_mod._lost_rockets_session = session_factory

    try:
        app = FastAPI()

        class FakeAuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                request.state.user = None
                request.state.session_token = None
                request.state.clear_session_cookie = False
                return await call_next(request)

        app.add_middleware(FakeAuthMiddleware)
        lost_rockets_mod.configure(templates=templates)
        app.include_router(lost_rockets_mod.router)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/lost-rockets")

        assert response.status_code == 200
        assert "Alice Rocketeer" in response.text
        assert "NXRS Spring Launch 2026" in response.text
        assert "blue" in response.text
        assert "G80T" in response.text
        assert "/events/2026/nxrs/record/10" in response.text
        assert "/events/2026/nxrs/images/uuid123-preflight.jpg" in response.text
    finally:
        lr_db_mod._lost_rockets_session = original_session
        await engine.dispose()


@pytest.mark.asyncio
async def test_lost_rockets_page_shows_approval_queue_for_admin(tmp_path: Path):
    """Admin users see the approval queue link."""
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates
    from starlette.middleware.base import BaseHTTPMiddleware
    from fastapi import Request

    from flight_card_scanner.routers import lost_rockets as lost_rockets_mod
    import flight_card_scanner.lost_rockets_database as lr_db_mod

    templates_dir = Path(__file__).resolve().parent.parent / "flight_card_scanner" / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))

    db_path = tmp_path / "lost_rockets.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(LostRocketsBase.metadata.create_all)

    original_session = lr_db_mod._lost_rockets_session
    lr_db_mod._lost_rockets_session = session_factory

    try:
        app = FastAPI()

        admin_user = MagicMock()
        admin_user.role = "admin"
        admin_user.email = "admin@test.com"
        admin_user.display_name = "Admin"

        class FakeAuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                request.state.user = admin_user
                request.state.session_token = None
                request.state.clear_session_cookie = False
                return await call_next(request)

        app.add_middleware(FakeAuthMiddleware)
        lost_rockets_mod.configure(templates=templates)
        app.include_router(lost_rockets_mod.router)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/lost-rockets")

        assert response.status_code == 200
        assert "/admin/preflight-queue" in response.text
        assert "Approval Queue" in response.text
    finally:
        lr_db_mod._lost_rockets_session = original_session
        await engine.dispose()


@pytest.mark.asyncio
async def test_lost_rockets_page_hides_approval_queue_for_flyer(tmp_path: Path):
    """Flyer users do not see the approval queue link."""
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates
    from starlette.middleware.base import BaseHTTPMiddleware

    from flight_card_scanner.routers import lost_rockets as lost_rockets_mod
    import flight_card_scanner.lost_rockets_database as lr_db_mod

    templates_dir = Path(__file__).resolve().parent.parent / "flight_card_scanner" / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))

    db_path = tmp_path / "lost_rockets.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(LostRocketsBase.metadata.create_all)

    original_session = lr_db_mod._lost_rockets_session
    lr_db_mod._lost_rockets_session = session_factory

    try:
        app = FastAPI()

        flyer_user = MagicMock()
        flyer_user.role = "flyer"
        flyer_user.email = "flyer@test.com"
        flyer_user.display_name = "Flyer"

        class FakeAuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                request.state.user = flyer_user
                request.state.session_token = None
                request.state.clear_session_cookie = False
                return await call_next(request)

        app.add_middleware(FakeAuthMiddleware)
        lost_rockets_mod.configure(templates=templates)
        app.include_router(lost_rockets_mod.router)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/lost-rockets")

        assert response.status_code == 200
        assert "/admin/preflight-queue" not in response.text
    finally:
        lr_db_mod._lost_rockets_session = original_session
        await engine.dispose()


@pytest.mark.asyncio
async def test_lost_rocket_entry_correct_data(tmp_path: Path):
    """Lost rocket entry stores correct data from flight record."""
    db_path = tmp_path / "lost_rockets.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(LostRocketsBase.metadata.create_all)

    # Create an entry with all fields populated
    async with session_factory() as session:
        entry = LostRocket(
            event_slug="2026/march",
            event_name="Missile Madness 2026",
            record_id=7,
            flier_name="Bob Builder",
            rocket_colors=["yellow", "black", "red"],
            diameter="2.6 in",
            length="18 in",
            motor_designation="F39T",
            flight_date=date(2026, 3, 16),
            preflight_image_path="deadbeef-preflight.jpg",
            added_by="bob@example.com",
        )
        session.add(entry)
        await session.commit()

    # Retrieve and verify
    async with session_factory() as session:
        result = await session.execute(
            select(LostRocket).where(
                LostRocket.event_slug == "2026/march",
                LostRocket.record_id == 7,
            )
        )
        rocket = result.scalar_one()

    assert rocket.event_name == "Missile Madness 2026"
    assert rocket.flier_name == "Bob Builder"
    assert rocket.rocket_colors == ["yellow", "black", "red"]
    assert rocket.diameter == "2.6 in"
    assert rocket.length == "18 in"
    assert rocket.motor_designation == "F39T"
    assert rocket.flight_date == date(2026, 3, 16)
    assert rocket.preflight_image_path == "deadbeef-preflight.jpg"
    assert rocket.added_by == "bob@example.com"
    # flight_date is a Monday
    assert rocket.flight_date.strftime("%A") == "Monday"

    await engine.dispose()
