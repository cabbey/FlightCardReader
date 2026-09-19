"""SQLAlchemy ORM models for the found rockets database.

Provides:
- ``FoundRocketsBase`` -- declarative base for found rockets models (separate DB)
- ``FoundRocket`` -- model representing a rocket that someone found in the field

Found rockets are reported directly by fliers (not derived from a flight
record) and live entirely outside the construct of a single launch event, so
this model — like ``LostRocket`` — uses its own declarative base and SQLite
file so entries persist across event database rotations.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# ---------------------------------------------------------------------------
# Declarative Base (separate from event DB, auth DB, and lost rockets DB)
# ---------------------------------------------------------------------------


class FoundRocketsBase(DeclarativeBase):
    """Declarative base for found rockets models.

    Intentionally separate from other databases so that found rocket entries
    persist across event DB rotations and live in their own SQLite file.
    """

    pass


# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------

STATUS_STILL_IN_FIELD = "still_in_field"
STATUS_RECOVERED = "recovered"
VALID_STATUSES = (STATUS_STILL_IN_FIELD, STATUS_RECOVERED)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class FoundRocket(FoundRocketsBase):
    """Represents a rocket that a flier found in the field.

    A finder uploads a photo of the rocket. GPS coordinates are extracted from
    the image EXIF metadata (if present) and used to default the location, but
    the finder can enter or adjust ``latitude``/``longitude`` manually. They can
    also leave a free-text ``description`` and record whether the rocket is
    ``still_in_field`` or has been ``recovered``.

    Images require admin approval before they are visible to anyone; until then
    the ``approved`` flag is False and the (tokenized, unguessable) image URL is
    not surfaced in any listing.

    When the rocket is reunited with its flier, ``reunited`` is set True which
    removes it from the public found rockets listing.
    """

    __tablename__ = "found_rockets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Location (decimal degrees). Defaulted from image EXIF GPS when available,
    # but always editable by the finder.
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)

    # "still_in_field" or "recovered"
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=STATUS_STILL_IN_FIELD
    )

    # Uploaded image: tokenized (unguessable) filename in the found rockets
    # image store, plus the raw token so the system can identify the record.
    image_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    image_token: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Admin approval gate for image visibility.
    approved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="0"
    )

    # Set True once reunited with the flier — removes it from the public list.
    reunited: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="0"
    )

    # The email of the flier who reported the found rocket.
    found_by: Mapped[str] = mapped_column(String(254), nullable=False)

    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
