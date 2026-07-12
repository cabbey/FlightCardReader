"""Per-event dependency injection for multi-event routing.

Provides FastAPI dependency functions that extract the event slug from
the URL path, look up the EventInfo from the EventManager, and provide
the event's session factory, config, and services to route handlers.
"""

from __future__ import annotations

from typing import AsyncGenerator

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..event_manager import EventInfo


async def get_event_info(request: Request, event_path: str) -> EventInfo:
    """FastAPI dependency that resolves the event_path to an EventInfo.

    Lazily opens the event if not already open. Returns 404 if the
    event slug is not found in the EventManager.
    """
    event_manager = request.app.state.event_manager

    # Normalize the slug (strip leading/trailing slashes)
    slug = event_path.strip("/")

    try:
        event_info = await event_manager.get_event(slug)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Event not found: {slug}")

    return event_info


async def get_event_db(request: Request, event_path: str) -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an AsyncSession for the event's database.

    Resolves the event from the URL path and provides its session factory.
    """
    event_info = await get_event_info(request, event_path)

    if event_info.session_factory is None:
        raise HTTPException(
            status_code=503,
            detail="Event database not available",
        )

    async with event_info.session_factory() as session:
        yield session
