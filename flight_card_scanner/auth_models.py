"""SQLAlchemy ORM models for the auth database.

Provides:
- ``AuthBase`` — declarative base for auth models (separate from event DB ``Base``)
- ``User`` — user account model
- ``Session`` — server-side session model
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# ---------------------------------------------------------------------------
# Declarative Base (separate from event DB Base)
# ---------------------------------------------------------------------------


class AuthBase(DeclarativeBase):
    """Declarative base for auth models.

    This is intentionally separate from the event database's ``Base`` so that
    auth tables live in their own SQLite file and persist across event DB
    rotations.
    """

    pass


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class User(AuthBase):
    """Represents a registered user account."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="data_entry")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Session(AuthBase):
    """Represents a server-side session tied to an authenticated user."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_active: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    client_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)

    __table_args__ = (
        Index("ix_sessions_user_id", "user_id"),
        Index("ix_sessions_last_active", "last_active"),
    )
