"""SQLAlchemy async engine, session factory, and table creation for the found rockets database.

Provides:
- ``FoundRocketsBase`` -- re-exported from ``found_rockets_models`` for convenience
- ``init_found_rockets_engine(db_path)`` -- configures the module-level engine and session factory
- ``get_found_rockets_db()`` -- FastAPI async dependency yielding an ``AsyncSession``
- ``create_found_rockets_tables(engine)`` -- creates all tables defined on ``FoundRocketsBase.metadata``
- ``migrate_found_rockets_columns(engine)`` -- adds columns introduced after initial schema creation
"""

from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .found_rockets_models import FoundRocketsBase  # single source of truth for metadata


# ---------------------------------------------------------------------------
# Module-level engine and session factory (configured at startup)
# ---------------------------------------------------------------------------

_found_rockets_engine: AsyncEngine | None = None
_found_rockets_session: async_sessionmaker[AsyncSession] | None = None


def init_found_rockets_engine(db_path: Path) -> AsyncEngine:
    """Create and store the async engine and session factory for the found rockets database.

    Call this once during application startup (e.g., in the FastAPI lifespan).

    Args:
        db_path: Filesystem path to the found rockets SQLite database file.

    Returns:
        The newly created ``AsyncEngine``.
    """
    global _found_rockets_engine, _found_rockets_session

    url = f"sqlite+aiosqlite:///{db_path}"
    _found_rockets_engine = create_async_engine(url, echo=False)
    _found_rockets_session = async_sessionmaker(
        _found_rockets_engine, expire_on_commit=False
    )
    return _found_rockets_engine


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


async def get_found_rockets_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an ``AsyncSession`` for the found rockets database.

    Usage::

        @router.get("/found-rockets")
        async def found_rockets(db: AsyncSession = Depends(get_found_rockets_db)):
            ...
    """
    if _found_rockets_session is None:
        raise RuntimeError(
            "Found rockets database session factory not initialised. "
            "Call init_found_rockets_engine() first."
        )
    async with _found_rockets_session() as session:
        yield session


# ---------------------------------------------------------------------------
# Schema initialisation helper
# ---------------------------------------------------------------------------


async def create_found_rockets_tables(engine: AsyncEngine) -> None:
    """Create all tables defined on ``FoundRocketsBase.metadata``.

    Uses ``run_sync`` to execute the synchronous DDL within an async context.

    Args:
        engine: The async engine to use for schema creation.
    """
    async with engine.begin() as conn:
        await conn.run_sync(FoundRocketsBase.metadata.create_all)


async def migrate_found_rockets_columns(engine: AsyncEngine) -> None:
    """Add columns introduced after initial schema creation and backfill data.

    Safely adds the ``image_token``, ``approved``, ``reunited``, and ``status``
    columns to the ``found_rockets`` table if they don't already exist, and
    backfills ``image_token`` for any legacy row that has an image but no token
    so those rows can be identified.

    Args:
        engine: The async engine to use for running ALTER TABLE statements.
    """
    import secrets

    from sqlalchemy import text

    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(found_rockets)"))
        existing_columns = {row[1] for row in result.fetchall()}

        if "image_token" not in existing_columns:
            await conn.execute(
                text(
                    "ALTER TABLE found_rockets ADD COLUMN image_token VARCHAR(64) DEFAULT NULL"
                )
            )

        if "approved" not in existing_columns:
            await conn.execute(
                text(
                    "ALTER TABLE found_rockets ADD COLUMN approved BOOLEAN NOT NULL DEFAULT 0"
                )
            )

        if "reunited" not in existing_columns:
            await conn.execute(
                text(
                    "ALTER TABLE found_rockets ADD COLUMN reunited BOOLEAN NOT NULL DEFAULT 0"
                )
            )

        if "status" not in existing_columns:
            await conn.execute(
                text(
                    "ALTER TABLE found_rockets ADD COLUMN status VARCHAR(32) "
                    "NOT NULL DEFAULT 'still_in_field'"
                )
            )

        # Backfill: generate tokens for existing rows that have an image but
        # lack a token. We do not rename files on disk here — the token merely
        # identifies migrated rows; visibility is handled by the ``approved``
        # flag at the template layer.
        result = await conn.execute(
            text(
                "SELECT id FROM found_rockets "
                "WHERE image_token IS NULL AND image_path IS NOT NULL"
            )
        )
        rows_to_backfill = result.fetchall()

        for row in rows_to_backfill:
            await conn.execute(
                text(
                    "UPDATE found_rockets SET image_token = :token WHERE id = :row_id"
                ),
                {"token": secrets.token_urlsafe(16), "row_id": row[0]},
            )
