"""Unit tests for the database module."""

import pytest
import pytest_asyncio
from pathlib import Path
from sqlalchemy import Column, Integer, String, select
from sqlalchemy.ext.asyncio import AsyncSession

from flight_card_scanner.database import (
    Base,
    create_all,
    get_db,
    get_engine,
    init_engine,
    _engine,
    _async_session,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def tmp_engine(tmp_path):
    """Initialise an engine pointing at a temp SQLite file."""
    db_path = tmp_path / "test.db"
    engine = init_engine(db_path)
    yield engine
    await engine.dispose()
    # Reset module state
    import flight_card_scanner.database as db_mod
    db_mod._engine = None
    db_mod._async_session = None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInitEngine:
    """Tests for init_engine."""

    def test_returns_engine(self, tmp_path):
        """init_engine returns an AsyncEngine."""
        from sqlalchemy.ext.asyncio import AsyncEngine

        db_path = tmp_path / "test.db"
        engine = init_engine(db_path)
        try:
            assert isinstance(engine, AsyncEngine)
        finally:
            import asyncio
            asyncio.get_event_loop().run_until_complete(engine.dispose())
            import flight_card_scanner.database as db_mod
            db_mod._engine = None
            db_mod._async_session = None

    def test_url_uses_aiosqlite(self, tmp_path):
        """The engine URL uses the sqlite+aiosqlite driver."""
        db_path = tmp_path / "test.db"
        engine = init_engine(db_path)
        try:
            assert "sqlite+aiosqlite" in str(engine.url)
        finally:
            import asyncio
            asyncio.get_event_loop().run_until_complete(engine.dispose())
            import flight_card_scanner.database as db_mod
            db_mod._engine = None
            db_mod._async_session = None


class TestGetEngine:
    """Tests for get_engine."""

    def test_raises_before_init(self):
        """get_engine raises RuntimeError if init_engine was not called."""
        import flight_card_scanner.database as db_mod
        old_engine = db_mod._engine
        db_mod._engine = None
        try:
            with pytest.raises(RuntimeError, match="not initialised"):
                get_engine()
        finally:
            db_mod._engine = old_engine

    @pytest.mark.asyncio
    async def test_returns_engine_after_init(self, tmp_engine):
        """get_engine returns the engine after init_engine is called."""
        engine = get_engine()
        assert engine is tmp_engine


class TestCreateAll:
    """Tests for create_all."""

    @pytest.mark.asyncio
    async def test_creates_tables(self, tmp_engine):
        """create_all creates tables from Base.metadata."""
        # Define a simple model for testing
        class _TestModel(Base):
            __tablename__ = "test_table"
            id = Column(Integer, primary_key=True)
            name = Column(String)

        await create_all(tmp_engine)

        # Verify the table exists by inserting and querying
        from sqlalchemy.ext.asyncio import async_sessionmaker

        Session = async_sessionmaker(tmp_engine, expire_on_commit=False)
        async with Session() as session:
            session.add(_TestModel(id=1, name="hello"))
            await session.commit()

        async with Session() as session:
            result = await session.execute(select(_TestModel))
            row = result.scalars().first()
            assert row is not None
            assert row.name == "hello"


class TestGetDb:
    """Tests for get_db dependency."""

    @pytest.mark.asyncio
    async def test_raises_before_init(self):
        """get_db raises RuntimeError if session factory is not set."""
        import flight_card_scanner.database as db_mod
        old_session = db_mod._async_session
        db_mod._async_session = None
        try:
            with pytest.raises(RuntimeError, match="not initialised"):
                async for _ in get_db():
                    pass
        finally:
            db_mod._async_session = old_session

    @pytest.mark.asyncio
    async def test_yields_async_session(self, tmp_engine):
        """get_db yields an AsyncSession."""
        async for session in get_db():
            assert isinstance(session, AsyncSession)
            break

    @pytest.mark.asyncio
    async def test_session_is_usable(self, tmp_engine):
        """The session from get_db can execute queries."""
        await create_all(tmp_engine)
        async for session in get_db():
            result = await session.execute(select(1))
            assert result.scalar() == 1
            break


class TestBase:
    """Tests for the Base class."""

    def test_is_declarative_base(self):
        """Base inherits from DeclarativeBase."""
        from sqlalchemy.orm import DeclarativeBase

        assert issubclass(Base, DeclarativeBase)
