"""Events router: top-level events listing and admin refresh endpoint.

Provides:
- GET / -- renders the events list page
- POST /api/admin/refresh-events -- re-scans events directory (admin only)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from ..auth_models import User
from ..dependencies.auth import Role, require_role

logger = logging.getLogger(__name__)

router = APIRouter()

# Module-level state set by configure()
_templates: Jinja2Templates | None = None


def configure(templates: Jinja2Templates) -> None:
    """Set module-level dependencies. Called once during app startup."""
    global _templates
    _templates = templates


@router.get("/", response_class=HTMLResponse)
async def events_list(request: Request) -> HTMLResponse:
    """Render the top-level events list page."""
    if _templates is None:
        raise RuntimeError("Events router not configured.")

    event_manager = request.app.state.event_manager
    events = event_manager.list_events()

    current_user = getattr(request.state, "user", None)

    # Query pending (inactive) user count for admin badge
    pending_count = 0
    if current_user and current_user.role == "admin":
        auth_service = getattr(request.app.state, "auth_service", None)
        if auth_service:
            async with auth_service._session_factory() as db:
                result = await db.execute(
                    select(func.count()).select_from(User).where(User.active == False)  # noqa: E712
                )
                pending_count = result.scalar() or 0

    return _templates.TemplateResponse(
        name="events_list.html",
        request=request,
        context={
            "page_title": "Events",
            "events": events,
            "current_user": current_user,
            "pending_count": pending_count,
        },
    )


@router.post(
    "/api/admin/refresh-events",
    dependencies=[Depends(require_role(Role.ADMIN))],
)
async def refresh_events(request: Request) -> dict:
    """Re-scan the events directory for new or removed events.

    Only admins can trigger this. Returns the updated event list.
    """
    event_manager = request.app.state.event_manager
    event_manager.refresh_events()
    await event_manager.gather_all_stats()
    events = event_manager.list_events()

    return {
        "message": f"Refreshed: {len(events)} event(s) found",
        "events": [
            {
                "slug": e.slug,
                "event_name": e.event_name,
                "is_open": e.is_open,
            }
            for e in events
        ],
    }
