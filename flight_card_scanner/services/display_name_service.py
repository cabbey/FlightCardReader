"""Display name resolver service with in-memory cache.

Translates user email addresses to their configured display names for
presentation in card history logs and other user-facing contexts.

The cache is populated from the auth database and refreshed on demand.
Lookups that miss the cache return the raw email address as a fallback.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flight_card_scanner.auth_models import User

logger = logging.getLogger(__name__)


class DisplayNameService:
    """Maintains an in-memory cache of email → display_name mappings.

    The cache is populated at startup via ``refresh()`` and can be
    refreshed at any time (e.g. after a user updates their profile).
    Individual entries can be invalidated via ``invalidate(email)`` or
    updated directly via ``update(email, display_name)``.
    """

    def __init__(self, session_factory: "async_sessionmaker[AsyncSession]") -> None:
        """Initialize the service.

        Args:
            session_factory: SQLAlchemy async session factory for the auth DB.
        """
        self._session_factory = session_factory
        self._cache: dict[str, str] = {}

    async def refresh(self) -> None:
        """Reload the entire cache from the auth database.

        Fetches all active users and populates the email → display_name map.
        """
        try:
            async with self._session_factory() as db:
                result = await db.execute(
                    select(User.email, User.display_name).where(
                        User.active == True  # noqa: E712
                    )
                )
                rows = result.all()

            new_cache: dict[str, str] = {}
            for email, display_name in rows:
                new_cache[email.lower()] = display_name

            self._cache = new_cache
            logger.info(
                "Display name cache refreshed: %d entries", len(self._cache)
            )
        except Exception as exc:
            logger.warning("Failed to refresh display name cache: %s", exc)

    def resolve(self, email: str) -> str:
        """Resolve an email address to a display name.

        Returns the cached display name if available, otherwise returns
        the email address unchanged as a fallback.

        Args:
            email: The email address to look up.

        Returns:
            The display name if found, otherwise the original email string.
        """
        return self._cache.get(email.lower(), email)

    def update(self, email: str, display_name: str) -> None:
        """Update a single cache entry (e.g. after a profile edit).

        Args:
            email: The user's email address.
            display_name: The new display name to cache.
        """
        self._cache[email.lower()] = display_name

    def invalidate(self, email: str) -> None:
        """Remove a single entry from the cache.

        Args:
            email: The email address to remove.
        """
        self._cache.pop(email.lower(), None)
