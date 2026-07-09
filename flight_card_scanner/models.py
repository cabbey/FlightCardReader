"""SQLAlchemy ORM models for the flight card scanner."""

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class FlightRecord(Base):
    """Represents a single flight card record extracted from a scanned image."""

    __tablename__ = "flight_records"

    # --- Identity ---
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # --- Image ---
    image_path: Mapped[str] = mapped_column(String(512), nullable=False)

    # --- Extraction lifecycle ---
    extraction_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", index=True
    )

    # --- Dedicated extracted columns ---
    flight_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    flier_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    total_impulse_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_impulse_unit: Mapped[str | None] = mapped_column(String(8), nullable=True)

    flag_heads_up: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    flag_first_flight: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    flag_complex: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    rack: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pad: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fso_rso_initials: Mapped[str | None] = mapped_column(String(16), nullable=True)

    evaluation_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    evaluation_comments: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Recovery plan ---
    recovery_plan: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # --- Flier verification ---
    flier_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    # --- Human review verification ---
    human_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    # --- JSON overflow for remaining fields ---
    overflow: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # --- Normalized metric values for measurement search (never displayed) ---
    norm_length_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    norm_diameter_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    norm_weight_g: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- Table-level indexes ---
    __table_args__ = (
        Index("ix_flight_records_created_at", created_at.desc()),
    )
