"""SQLAlchemy async engine, session factory, and Base.

Provides:
- ``Base`` — declarative base for ORM models
- ``init_engine(db_path)`` — configures the module-level engine and session factory
- ``get_db()`` — FastAPI async dependency yielding an ``AsyncSession``
- ``create_all(engine)`` — creates all tables defined on ``Base.metadata``
"""

from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

# ---------------------------------------------------------------------------
# Declarative Base
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""

    pass


# ---------------------------------------------------------------------------
# Module-level engine and session factory (configured at startup)
# ---------------------------------------------------------------------------

_engine: AsyncEngine | None = None
_async_session: async_sessionmaker[AsyncSession] | None = None


def init_engine(db_path: Path, read_only: bool = False) -> AsyncEngine:
    """Create and store the async engine and session factory.

    Call this once during application startup (e.g., in the FastAPI lifespan).

    Args:
        db_path: Filesystem path to the SQLite database file.
        read_only: If True, open the database in read-only mode (SQLite URI mode).

    Returns:
        The newly created ``AsyncEngine``.
    """
    global _engine, _async_session

    if read_only:
        # Use SQLite URI mode with ?mode=ro for true read-only access
        uri_path = str(db_path).replace("?", "%3f").replace("#", "%23")
        url = f"sqlite+aiosqlite:///file:{uri_path}?mode=ro&uri=true"
    else:
        url = f"sqlite+aiosqlite:///{db_path}"
    _engine = create_async_engine(url, echo=False)
    _async_session = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_engine() -> AsyncEngine:
    """Return the current engine, raising if not yet initialised."""
    if _engine is None:
        raise RuntimeError(
            "Database engine not initialised. Call init_engine() first."
        )
    return _engine


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an ``AsyncSession``.

    Usage::

        @router.post("/scan")
        async def scan(db: AsyncSession = Depends(get_db)):
            ...
    """
    if _async_session is None:
        raise RuntimeError(
            "Database session factory not initialised. Call init_engine() first."
        )
    async with _async_session() as session:
        yield session


# ---------------------------------------------------------------------------
# Schema initialisation helper
# ---------------------------------------------------------------------------


async def create_all(engine: AsyncEngine) -> None:
    """Create all tables defined on ``Base.metadata``.

    Uses ``run_sync`` to execute the synchronous DDL within an async context.

    Args:
        engine: The async engine to use for schema creation.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def migrate_add_columns(engine: AsyncEngine) -> None:
    """Add new columns to existing tables if they don't already exist.

    This handles the case where create_all created the table in a previous
    version and new columns have been added to the model. SQLite does not
    support IF NOT EXISTS for ALTER TABLE ADD COLUMN, so we inspect the
    table's column list first.

    Call this after create_all during application startup.

    Args:
        engine: The async engine to use for migration.
    """
    from sqlalchemy import text

    migrations = [
        # (table_name, column_name, column_ddl)
        ("flight_records", "back_image_path", "VARCHAR(512) DEFAULT NULL"),
    ]

    async with engine.begin() as conn:
        for table_name, col_name, col_ddl in migrations:
            # Check if column already exists by querying table_info
            result = await conn.execute(
                text(f"PRAGMA table_info({table_name})")
            )
            columns = [row[1] for row in result.fetchall()]
            if col_name not in columns:
                await conn.execute(
                    text(
                        f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_ddl}"
                    )
                )
