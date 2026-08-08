"""SQLAlchemy ORM models for the lost rockets database.

Provides:
- ``LostRocketsBase`` -- declarative base for lost rockets models (separate DB)
- ``LostRocket`` -- model representing a rocket marked as lost
"""

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# ---------------------------------------------------------------------------
# Declarative Base (separate from event DB and auth DB)
# ---------------------------------------------------------------------------


class LostRocketsBase(DeclarativeBase):
    """Declarative base for lost rockets models.

    This is intentionally separate from other databases so that lost rocket
    entries persist across event DB rotations and live in their own SQLite file.
    """

    pass


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class LostRocket(LostRocketsBase):
    """Represents a rocket that has been marked as lost.

    NOTE: This model denormalizes flier_name, rocket_colors, diameter, length,
    and motor_designation from the source FlightRecord at insertion time. If a
    data_entry user later corrects those fields on the flight record, the lost
    rockets listing will show outdated information. This is a known limitation
    acceptable for the MVP. A future improvement could join back to the source
    event DB at query time or implement a sync mechanism.
    """

    __tablename__ = "lost_rockets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_slug: Mapped[str] = mapped_column(String(256), nullable=False)
    event_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    record_id: Mapped[int] = mapped_column(Integer, nullable=False)
    flier_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    rocket_colors: Mapped[list | None] = mapped_column(JSON, nullable=True)
    diameter: Mapped[str | None] = mapped_column(String(64), nullable=True)
    length: Mapped[str | None] = mapped_column(String(64), nullable=True)
    motor_designation: Mapped[str | None] = mapped_column(String(128), nullable=True)
    flight_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    preflight_image_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    preflight_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    added_by: Mapped[str] = mapped_column(String(254), nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("event_slug", "record_id", name="uq_event_record"),
    )
