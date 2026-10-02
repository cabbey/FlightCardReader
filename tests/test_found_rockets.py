"""Tests for the Found Rockets feature.

Covers:
- FoundRocket model / database creation and migration
- EXIF GPS extraction service
- Found rocket image service (tokenized filenames)
- Submit flow (image upload, EXIF-defaulted coords, manual override)
- Reunite authorization (poster or admin) and list exclusion
- Image serving gated by approval (unguessable + unapproved => 404)
- Unified approval queue includes found rockets; approve/delete endpoints
"""

import io
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from flight_card_scanner.found_rockets_database import (
    create_found_rockets_tables,
    migrate_found_rockets_columns,
)
from flight_card_scanner.found_rockets_models import FoundRocket, FoundRocketsBase


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(email="flyer@test.com", role="flyer", display_name="Flyer"):
    user = MagicMock()
    user.email = email
    user.role = role
    user.display_name = display_name
    user.active = True
    return user


def _make_jpeg_with_gps(lat=43.79913, lon=-103.5545) -> bytes:
    """Create a small JPEG carrying GPS EXIF for the given coordinates."""
    from PIL import Image
    from PIL.ExifTags import IFD
    from PIL.TiffImagePlugin import IFDRational

    img = Image.new("RGB", (16, 16), color=(120, 80, 40))
    exif = img.getexif()
    gps = exif.get_ifd(IFD.GPSInfo)

    def to_dms(value):
        value = abs(value)
        deg = int(value)
        minf = (value - deg) * 60
        minute = int(minf)
        sec = round((minf - minute) * 60, 4)
        return (
            IFDRational(deg, 1),
            IFDRational(minute, 1),
            IFDRational(int(sec * 10000), 10000),
        )

    # GPS tag ids: 1=LatRef 2=Lat 3=LonRef 4=Lon
    gps[1] = "N" if lat >= 0 else "S"
    gps[2] = to_dms(lat)
    gps[3] = "E" if lon >= 0 else "W"
    gps[4] = to_dms(lon)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def _make_plain_jpeg() -> bytes:
    from PIL import Image

    img = Image.new("RGB", (16, 16), color=(10, 200, 10))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


async def _make_found_db(tmp_path: Path):
    db_path = tmp_path / "found_rockets.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(FoundRocketsBase.metadata.create_all)
    return engine, session_factory


def _templates() -> Jinja2Templates:
    templates_dir = Path(__file__).parent.parent / "flight_card_scanner" / "templates"
    return Jinja2Templates(directory=str(templates_dir))


async def _build_app(tmp_path: Path, user, session_factory):
    """Build a FastAPI app wired with the found_rockets router for testing."""
    from flight_card_scanner import found_rockets_database as fr_db
    from flight_card_scanner.routers import found_rockets as fr_router

    images_dir = tmp_path / "found_images"
    images_dir.mkdir(exist_ok=True)

    # Point the module-level session factory at our test DB.
    fr_db._found_rockets_session = session_factory
    fr_router.configure(templates=_templates(), images_path=images_dir)

    app = FastAPI()

    class FakeAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.user = user
            return await call_next(request)

    app.add_middleware(FakeAuthMiddleware)
    app.state.app_config = MagicMock(found_rockets_images_path=images_dir)
    app.include_router(fr_router.router)
    return app, images_dir


# ---------------------------------------------------------------------------
# Model / database
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_migrate_tables(tmp_path: Path):
    engine, session_factory = await _make_found_db(tmp_path)
    # Migration should be idempotent on an already-created table.
    await migrate_found_rockets_columns(engine)
    async with session_factory() as session:
        session.add(
            FoundRocket(
                description="red 4in",
                latitude=1.5,
                longitude=-2.5,
                status="in_field",
                image_path="found-x-tok.jpg",
                image_token="tok",
                found_by="a@test.com",
            )
        )
        await session.commit()
        rows = (await session.execute(select(FoundRocket))).scalars().all()
        assert len(rows) == 1
        assert rows[0].approved is False
        assert rows[0].status == "in_field"
    await engine.dispose()


@pytest.mark.asyncio
async def test_migration_collapses_legacy_status_and_reunited(tmp_path: Path):
    """The old (status='still_in_field'/'recovered' + reunited bool) rows fold
    into the single status field, while approved stays a separate column."""
    from sqlalchemy import text

    db_path = tmp_path / "legacy_found.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    # Build the OLD schema (pre-collapse): approved + reunited + two-value status.
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE found_rockets ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "description TEXT, latitude FLOAT, longitude FLOAT, "
                "status VARCHAR(32) NOT NULL DEFAULT 'still_in_field', "
                "image_path VARCHAR(512), image_token VARCHAR(64), "
                "approved BOOLEAN NOT NULL DEFAULT 0, "
                "reunited BOOLEAN NOT NULL DEFAULT 0, "
                "found_by VARCHAR(254) NOT NULL, "
                "added_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
        )
        # Row A: still in field, approved, not reunited -> in_field, approved
        # Row B: recovered, approved, not reunited      -> recovered, approved
        # Row C: reunited (was recovered)               -> reunited
        # Row D: still in field, NOT approved           -> in_field, not approved
        await conn.execute(
            text(
                "INSERT INTO found_rockets "
                "(status, approved, reunited, found_by, image_path, image_token) VALUES "
                "('still_in_field', 1, 0, 'a@t.com', 'a.jpg', 'ta'), "
                "('recovered', 1, 0, 'b@t.com', 'b.jpg', 'tb'), "
                "('recovered', 1, 1, 'c@t.com', 'c.jpg', 'tc'), "
                "('still_in_field', 0, 0, 'd@t.com', 'd.jpg', 'td')"
            )
        )

    # Run the real migration.
    await migrate_found_rockets_columns(engine)

    async with engine.begin() as conn:
        result = await conn.execute(
            text("SELECT found_by, status, approved FROM found_rockets ORDER BY id")
        )
        rows = {r[0]: (r[1], bool(r[2])) for r in result.fetchall()}

    assert rows["a@t.com"] == ("in_field", True)
    assert rows["b@t.com"] == ("recovered", True)
    assert rows["c@t.com"] == ("reunited", True)
    assert rows["d@t.com"] == ("in_field", False)
    await engine.dispose()


# ---------------------------------------------------------------------------
# EXIF extraction
# ---------------------------------------------------------------------------


def test_exif_extracts_gps():
    from flight_card_scanner.services.exif_service import extract_gps_coordinates

    coords = extract_gps_coordinates(_make_jpeg_with_gps(43.79913, -103.5545))
    assert coords is not None
    assert coords.latitude == pytest.approx(43.79913, abs=1e-3)
    assert coords.longitude == pytest.approx(-103.5545, abs=1e-3)


def test_exif_no_gps_returns_none():
    from flight_card_scanner.services.exif_service import extract_gps_coordinates

    assert extract_gps_coordinates(_make_plain_jpeg()) is None


def test_exif_malformed_returns_none():
    from flight_card_scanner.services.exif_service import extract_gps_coordinates

    assert extract_gps_coordinates(b"not an image") is None


# ---------------------------------------------------------------------------
# Image service
# ---------------------------------------------------------------------------


def test_image_service_tokenized_filename(tmp_path: Path):
    from flight_card_scanner.services.found_rocket_image_service import (
        generate_image_token,
        save_found_rocket_image,
    )

    token = generate_image_token()
    fname = save_found_rocket_image(b"\xff\xd8\xff\x00", "jpg", tmp_path, token)
    assert token in fname
    assert fname.endswith(".jpg")
    assert (tmp_path / fname).exists()


# ---------------------------------------------------------------------------
# Submit flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_defaults_coords_from_exif(tmp_path: Path):
    engine, session_factory = await _make_found_db(tmp_path)
    app, images_dir = await _build_app(tmp_path, _make_user(), session_factory)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/found-rockets/api/report",
            data={"description": "silver 3in", "status": "in_field"},
            files={"image": ("r.jpg", _make_jpeg_with_gps(43.79913, -103.5545), "image/jpeg")},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["latitude"] == pytest.approx(43.79913, abs=1e-3)
    assert body["longitude"] == pytest.approx(-103.5545, abs=1e-3)

    async with session_factory() as session:
        rocket = (await session.execute(select(FoundRocket))).scalar_one()
        assert rocket.approved is False  # pending approval
        assert rocket.description == "silver 3in"
        assert rocket.image_path and rocket.image_token
        assert (images_dir / rocket.image_path).exists()

    await engine.dispose()


@pytest.mark.asyncio
async def test_submit_manual_coords_override_exif(tmp_path: Path):
    engine, session_factory = await _make_found_db(tmp_path)
    app, _ = await _build_app(tmp_path, _make_user(), session_factory)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/found-rockets/api/report",
            data={"status": "recovered", "latitude": "10.0", "longitude": "20.0"},
            files={"image": ("r.jpg", _make_jpeg_with_gps(43.7, -103.5), "image/jpeg")},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["latitude"] == pytest.approx(10.0)
    assert body["longitude"] == pytest.approx(20.0)
    assert body["status"] == "recovered"
    await engine.dispose()


@pytest.mark.asyncio
async def test_submit_rejects_non_image(tmp_path: Path):
    engine, session_factory = await _make_found_db(tmp_path)
    app, _ = await _build_app(tmp_path, _make_user(), session_factory)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/found-rockets/api/report",
            data={"status": "in_field"},
            files={"image": ("r.txt", b"hello", "text/plain")},
        )
    assert resp.status_code == 400
    await engine.dispose()


@pytest.mark.asyncio
async def test_extract_gps_endpoint(tmp_path: Path):
    engine, session_factory = await _make_found_db(tmp_path)
    app, _ = await _build_app(tmp_path, _make_user(), session_factory)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/found-rockets/api/extract-gps",
            files={"image": ("r.jpg", _make_jpeg_with_gps(1.0, 2.0), "image/jpeg")},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["found"] is True
    assert data["latitude"] == pytest.approx(1.0, abs=1e-3)
    await engine.dispose()


# ---------------------------------------------------------------------------
# Image serving gated by approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unapproved_image_hidden_from_public(tmp_path: Path):
    engine, session_factory = await _make_found_db(tmp_path)
    # Public (anonymous) user
    app, images_dir = await _build_app(tmp_path, None, session_factory)

    # Seed an unapproved rocket with a real image file on disk.
    (images_dir / "found-secret-tok.jpg").write_bytes(b"\xff\xd8\xff\x00")
    async with session_factory() as session:
        session.add(
            FoundRocket(
                image_path="found-secret-tok.jpg",
                image_token="tok",
                approved=False,
                found_by="flyer@test.com",
            )
        )
        await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get("/found-rockets/images/found-secret-tok.jpg")
    # Unapproved + non-admin => 404, hiding existence entirely.
    assert resp.status_code == 404
    await engine.dispose()


@pytest.mark.asyncio
async def test_approved_image_served(tmp_path: Path):
    engine, session_factory = await _make_found_db(tmp_path)
    app, images_dir = await _build_app(tmp_path, None, session_factory)

    (images_dir / "found-ok-tok.jpg").write_bytes(b"\xff\xd8\xff\x00")
    async with session_factory() as session:
        session.add(
            FoundRocket(
                image_path="found-ok-tok.jpg",
                image_token="tok",
                approved=True,
                found_by="flyer@test.com",
            )
        )
        await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get("/found-rockets/images/found-ok-tok.jpg")
    assert resp.status_code == 200
    await engine.dispose()


# ---------------------------------------------------------------------------
# Reunite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reunite_by_poster(tmp_path: Path):
    engine, session_factory = await _make_found_db(tmp_path)
    async with session_factory() as session:
        session.add(FoundRocket(image_path="a.jpg", image_token="t", found_by="flyer@test.com"))
        await session.commit()
        rid = (await session.execute(select(FoundRocket.id))).scalar_one()

    app, _ = await _build_app(tmp_path, _make_user(email="flyer@test.com"), session_factory)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.post(f"/found-rockets/api/{rid}/reunite")
    assert resp.status_code == 200

    async with session_factory() as session:
        rocket = (await session.execute(select(FoundRocket))).scalar_one()
        assert rocket.status == "reunited"
    await engine.dispose()


@pytest.mark.asyncio
async def test_reunite_forbidden_for_other_flyer(tmp_path: Path):
    engine, session_factory = await _make_found_db(tmp_path)
    async with session_factory() as session:
        session.add(FoundRocket(image_path="a.jpg", image_token="t", found_by="owner@test.com"))
        await session.commit()
        rid = (await session.execute(select(FoundRocket.id))).scalar_one()

    app, _ = await _build_app(tmp_path, _make_user(email="someone@test.com"), session_factory)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.post(f"/found-rockets/api/{rid}/reunite")
    assert resp.status_code == 403
    await engine.dispose()


@pytest.mark.asyncio
async def test_reunite_by_admin(tmp_path: Path):
    engine, session_factory = await _make_found_db(tmp_path)
    async with session_factory() as session:
        session.add(FoundRocket(image_path="a.jpg", image_token="t", found_by="owner@test.com"))
        await session.commit()
        rid = (await session.execute(select(FoundRocket.id))).scalar_one()

    app, _ = await _build_app(tmp_path, _make_user(email="admin@test.com", role="admin"), session_factory)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.post(f"/found-rockets/api/{rid}/reunite")
    assert resp.status_code == 200
    await engine.dispose()


@pytest.mark.asyncio
async def test_reunited_excluded_from_listing(tmp_path: Path):
    engine, session_factory = await _make_found_db(tmp_path)
    async with session_factory() as session:
        session.add(FoundRocket(image_path="a.jpg", image_token="t", approved=True, status="reunited", found_by="o@test.com", description="GONE-REUNITED"))
        session.add(FoundRocket(image_path="b.jpg", image_token="t2", approved=True, status="in_field", found_by="o@test.com", description="VISIBLE-ONE"))
        await session.commit()

    app, _ = await _build_app(tmp_path, _make_user(role="admin"), session_factory)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get("/found-rockets")
    assert resp.status_code == 200
    assert "VISIBLE-ONE" in resp.text
    assert "GONE-REUNITED" not in resp.text
    await engine.dispose()



# ---------------------------------------------------------------------------
# Unified approval queue (auth router endpoints for found rockets)
# ---------------------------------------------------------------------------


async def _build_queue_app(tmp_path: Path, user, session_factory, images_dir):
    """Wire the auth router's found-rocket approve/delete endpoints for testing."""
    from flight_card_scanner import found_rockets_database as fr_db
    from flight_card_scanner.routers import auth as auth_router
    from flight_card_scanner.services.auth_service import AuthService

    fr_db._found_rockets_session = session_factory

    # Minimal AuthService (only needs a session factory attribute for unused paths)
    auth_service = MagicMock(spec=AuthService)
    auth_router.configure(
        auth_service=auth_service, templates=_templates(), session_middleware=None
    )

    app = FastAPI()

    class FakeAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.user = user
            return await call_next(request)

    app.add_middleware(FakeAuthMiddleware)
    app.state.app_config = MagicMock(found_rockets_images_path=images_dir)
    app.state.event_manager = MagicMock(events={})
    app.include_router(auth_router.router)
    return app


@pytest.mark.asyncio
async def test_queue_includes_found_rockets(tmp_path: Path):
    engine, session_factory = await _make_found_db(tmp_path)
    images_dir = tmp_path / "found_images"
    images_dir.mkdir()
    (images_dir / "found-q-tok.jpg").write_bytes(b"\xff\xd8\xff\x00")
    async with session_factory() as session:
        session.add(
            FoundRocket(
                description="QUEUE-DESC",
                image_path="found-q-tok.jpg",
                image_token="tok",
                approved=False,
                found_by="flyer@test.com",
            )
        )
        await session.commit()

    app = await _build_queue_app(
        tmp_path, _make_user(role="data_entry"), session_factory, images_dir
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get("/admin/preflight-queue")
    assert resp.status_code == 200
    assert "QUEUE-DESC" in resp.text
    assert "/found-rockets/images/found-q-tok.jpg" in resp.text
    await engine.dispose()


@pytest.mark.asyncio
async def test_queue_approve_found_rocket(tmp_path: Path):
    engine, session_factory = await _make_found_db(tmp_path)
    images_dir = tmp_path / "found_images"
    images_dir.mkdir()
    async with session_factory() as session:
        session.add(FoundRocket(image_path="a.jpg", image_token="t", approved=False, found_by="f@test.com"))
        await session.commit()
        rid = (await session.execute(select(FoundRocket.id))).scalar_one()

    app = await _build_queue_app(
        tmp_path, _make_user(role="data_entry"), session_factory, images_dir
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.post(f"/api/admin/found-rockets/{rid}/approve")
    assert resp.status_code == 200

    async with session_factory() as session:
        rocket = (await session.execute(select(FoundRocket))).scalar_one()
        assert rocket.approved is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_queue_delete_found_rocket(tmp_path: Path):
    engine, session_factory = await _make_found_db(tmp_path)
    images_dir = tmp_path / "found_images"
    images_dir.mkdir()
    (images_dir / "found-del-tok.jpg").write_bytes(b"\xff\xd8\xff\x00")
    async with session_factory() as session:
        session.add(FoundRocket(image_path="found-del-tok.jpg", image_token="t", approved=False, found_by="f@test.com"))
        await session.commit()
        rid = (await session.execute(select(FoundRocket.id))).scalar_one()

    app = await _build_queue_app(
        tmp_path, _make_user(role="data_entry"), session_factory, images_dir
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.post(f"/api/admin/found-rockets/{rid}/delete")
    assert resp.status_code == 200

    async with session_factory() as session:
        rows = (await session.execute(select(FoundRocket))).scalars().all()
        assert rows == []
    assert not (images_dir / "found-del-tok.jpg").exists()
    await engine.dispose()


@pytest.mark.asyncio
async def test_queue_approve_found_rocket_forbidden_for_flyer(tmp_path: Path):
    engine, session_factory = await _make_found_db(tmp_path)
    images_dir = tmp_path / "found_images"
    images_dir.mkdir()
    async with session_factory() as session:
        session.add(FoundRocket(image_path="a.jpg", image_token="t", approved=False, found_by="f@test.com"))
        await session.commit()
        rid = (await session.execute(select(FoundRocket.id))).scalar_one()

    app = await _build_queue_app(
        tmp_path, _make_user(role="flyer"), session_factory, images_dir
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.post(f"/api/admin/found-rockets/{rid}/approve")
    assert resp.status_code == 403
    await engine.dispose()
