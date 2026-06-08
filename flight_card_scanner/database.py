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


def init_engine(db_path: Path) -> AsyncEngine:
    """Create and store the async engine and session factory.

    Call this once during application startup (e.g., in the FastAPI lifespan).

    Args:
        db_path: Filesystem path to the SQLite database file.

    Returns:
        The newly created ``AsyncEngine``.
    """
    global _engine, _async_session

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
