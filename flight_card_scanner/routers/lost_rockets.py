"""Lost Rockets router: public listing of all rockets marked as lost across events.

Provides:
- GET /lost-rockets -- renders the lost rockets page (visible to all)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..lost_rockets_database import get_lost_rockets_db
from ..lost_rockets_models import LostRocket

logger = logging.getLogger(__name__)

router = APIRouter()

# Module-level state set by configure()
_templates: Jinja2Templates | None = None


def configure(templates: Jinja2Templates) -> None:
    """Set module-level dependencies. Called once during app startup."""
    global _templates
    _templates = templates


@router.get("/lost-rockets", response_class=HTMLResponse)
async def lost_rockets_page(
    request: Request,
    db: AsyncSession = Depends(get_lost_rockets_db),
) -> HTMLResponse:
    """Render the lost rockets listing page."""
    if _templates is None:
        raise RuntimeError("Lost rockets router not configured.")

    result = await db.execute(
        select(LostRocket).order_by(LostRocket.added_at.desc())
    )
    rockets = result.scalars().all()

    current_user = getattr(request.state, "user", None)

    return _templates.TemplateResponse(
        name="lost_rockets.html",
        request=request,
        context={
            "page_title": "Lost Rockets",
            "rockets": rockets,
            "current_user": current_user,
        },
    )
