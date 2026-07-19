"""SQLAlchemy async engine, session factory, and table creation for the auth database.

Provides:
- ``AuthBase`` — re-exported from ``auth_models`` for convenience
- ``init_auth_engine(db_path)`` — configures the module-level engine and session factory
- ``get_auth_db()`` — FastAPI async dependency yielding an ``AsyncSession``
- ``create_auth_tables(engine)`` — creates all tables defined on ``AuthBase.metadata``

Note: ``AuthBase`` is defined in ``auth_models.py`` and imported here so that
models (User, Session) and this module share a single metadata registry.
"""

from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .auth_models import AuthBase  # single source of truth for auth metadata


# ---------------------------------------------------------------------------
# Module-level engine and session factory (configured at startup)
# ---------------------------------------------------------------------------

_auth_engine: AsyncEngine | None = None
_auth_session: async_sessionmaker[AsyncSession] | None = None


def init_auth_engine(db_path: Path) -> AsyncEngine:
    """Create and store the async engine and session factory for the auth database.

    Call this once during application startup (e.g., in the FastAPI lifespan).

    Args:
        db_path: Filesystem path to the auth SQLite database file.

    Returns:
        The newly created ``AsyncEngine``.
    """
    global _auth_engine, _auth_session

    url = f"sqlite+aiosqlite:///{db_path}"
    _auth_engine = create_async_engine(url, echo=False)
    _auth_session = async_sessionmaker(_auth_engine, expire_on_commit=False)
    return _auth_engine


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


async def get_auth_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an ``AsyncSession`` for the auth database.

    Usage::

        @router.post("/login")
        async def login(db: AsyncSession = Depends(get_auth_db)):
            ...
    """
    if _auth_session is None:
        raise RuntimeError(
            "Auth database session factory not initialised. "
            "Call init_auth_engine() first."
        )
    async with _auth_session() as session:
        yield session


# ---------------------------------------------------------------------------
# Schema initialisation helper
# ---------------------------------------------------------------------------


async def create_auth_tables(engine: AsyncEngine) -> None:
    """Create all tables defined on ``AuthBase.metadata``.

    Uses ``run_sync`` to execute the synchronous DDL within an async context.

    Args:
        engine: The async engine to use for schema creation.
    """
    async with engine.begin() as conn:
        await conn.run_sync(AuthBase.metadata.create_all)
