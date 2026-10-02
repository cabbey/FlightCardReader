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
# Status lifecycle
# ---------------------------------------------------------------------------
#
# A found rocket's disposition is captured by a single ``status`` field. This
# collapses what were previously two fields — a two-value ``status``
# (``still_in_field``/``recovered``) and a ``reunited`` boolean — into one set
# of mutually exclusive states:
#
#   in_field   -- believed to still be out in the field.
#   recovered  -- physically recovered (with the finder or in the club
#                 lost & found).
#   reunited   -- returned to its flier; removed from the public list.
#
# Admin approval of the uploaded photo is a *separate* concern tracked by the
# ``approved`` boolean (it gates image visibility), not part of ``status``.
#
STATUS_IN_FIELD = "in_field"
STATUS_RECOVERED = "recovered"
STATUS_REUNITED = "reunited"

# All persisted status values.
VALID_STATUSES = (STATUS_IN_FIELD, STATUS_RECOVERED, STATUS_REUNITED)

# Statuses a finder may pick when they report a rocket (reunited is only reached
# later, via the reunite action).
REPORTABLE_STATUSES = (STATUS_IN_FIELD, STATUS_RECOVERED)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class FoundRocket(FoundRocketsBase):
    """Represents a rocket that a flier found in the field.

    A finder uploads a photo of the rocket. GPS coordinates are extracted from
    the image EXIF metadata (if present) and used to default the location, but
    the finder can enter or adjust ``latitude``/``longitude`` manually. They can
    also leave a free-text ``description`` and record the rocket's condition.

    The rocket's disposition is captured by the single ``status`` field
    (``in_field`` / ``recovered`` / ``reunited`` — see the status constants
    above), which collapses the former ``status`` + ``reunited`` fields. Once a
    rocket is ``reunited`` it is removed from the public listing.

    Whether the uploaded photo has been approved by an admin is tracked
    separately by ``approved``: until then the rocket is not visible to the
    public and the (tokenized, unguessable) image URL is not served to
    non-admins.
    """

    __tablename__ = "found_rockets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Location (decimal degrees). Defaulted from image EXIF GPS when available,
    # but always editable by the finder.
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Single disposition status: in_field / recovered / reunited.
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=STATUS_IN_FIELD
    )

    # Uploaded image: tokenized (unguessable) filename in the found rockets
    # image store, plus the raw token so the system can identify the record.
    image_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    image_token: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Admin approval gate for image visibility (separate from ``status``).
    approved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="0"
    )

    # The email of the flier who reported the found rocket.
    found_by: Mapped[str] = mapped_column(String(254), nullable=False)

    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # -- Convenience helpers -------------------------------------------------

    @property
    def is_reunited(self) -> bool:
        return self.status == STATUS_REUNITED
