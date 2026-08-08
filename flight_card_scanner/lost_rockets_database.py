"""SQLAlchemy async engine, session factory, and table creation for the lost rockets database.

Provides:
- ``LostRocketsBase`` -- re-exported from ``lost_rockets_models`` for convenience
- ``init_lost_rockets_engine(db_path)`` -- configures the module-level engine and session factory
- ``get_lost_rockets_db()`` -- FastAPI async dependency yielding an ``AsyncSession``
- ``create_lost_rockets_tables(engine)`` -- creates all tables defined on ``LostRocketsBase.metadata``
"""

from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .lost_rockets_models import LostRocketsBase  # single source of truth for metadata


# ---------------------------------------------------------------------------
# Module-level engine and session factory (configured at startup)
# ---------------------------------------------------------------------------

_lost_rockets_engine: AsyncEngine | None = None
_lost_rockets_session: async_sessionmaker[AsyncSession] | None = None


def init_lost_rockets_engine(db_path: Path) -> AsyncEngine:
    """Create and store the async engine and session factory for the lost rockets database.

    Call this once during application startup (e.g., in the FastAPI lifespan).

    Args:
        db_path: Filesystem path to the lost rockets SQLite database file.

    Returns:
        The newly created ``AsyncEngine``.
    """
    global _lost_rockets_engine, _lost_rockets_session

    url = f"sqlite+aiosqlite:///{db_path}"
    _lost_rockets_engine = create_async_engine(url, echo=False)
    _lost_rockets_session = async_sessionmaker(_lost_rockets_engine, expire_on_commit=False)
    return _lost_rockets_engine


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


async def get_lost_rockets_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an ``AsyncSession`` for the lost rockets database.

    Usage::

        @router.get("/lost-rockets")
        async def lost_rockets(db: AsyncSession = Depends(get_lost_rockets_db)):
            ...
    """
    if _lost_rockets_session is None:
        raise RuntimeError(
            "Lost rockets database session factory not initialised. "
            "Call init_lost_rockets_engine() first."
        )
    async with _lost_rockets_session() as session:
        yield session


# ---------------------------------------------------------------------------
# Schema initialisation helper
# ---------------------------------------------------------------------------


async def create_lost_rockets_tables(engine: AsyncEngine) -> None:
    """Create all tables defined on ``LostRocketsBase.metadata``.

    Uses ``run_sync`` to execute the synchronous DDL within an async context.

    Args:
        engine: The async engine to use for schema creation.
    """
    async with engine.begin() as conn:
        await conn.run_sync(LostRocketsBase.metadata.create_all)


async def migrate_lost_rockets_columns(engine: AsyncEngine) -> None:
    """Run lightweight column migrations for the lost rockets database.

    Adds any new columns that don't yet exist in the lost_rockets table.
    Call this after create_lost_rockets_tables during startup.

    Args:
        engine: The async engine to use for migration.
    """
    from sqlalchemy import text

    migrations = [
        # (table_name, column_name, column_ddl)
        ("lost_rockets", "preflight_approved", "BOOLEAN NOT NULL DEFAULT 0"),
    ]

    async with engine.begin() as conn:
        for table_name, col_name, col_ddl in migrations:
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
