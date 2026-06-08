"""Pydantic request/response and LLM output schemas.

Defines:
- LLM structured output models (passed to Ollama as the `format` JSON Schema parameter)
- API request/response models for the FastAPI endpoints
"""

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field


# ─── LLM Structured Output Schema ────────────────────────────────────────────


class MembershipInfo(BaseModel):
    """Flier's rocketry club membership details."""

    club: Optional[str] = Field(None, description="TRA, NAR, or CAR")
    member_number: Optional[str] = None
    cert_level: Optional[int] = Field(None, ge=0, le=4)


class RocketMeasurements(BaseModel):
    """Physical measurements of the rocket."""

    diameter: Optional[float] = None
    diameter_unit: Optional[str] = None
    length: Optional[float] = None
    length_unit: Optional[str] = None
    weight: Optional[float] = None
    weight_unit: Optional[str] = None


class MotorEntry(BaseModel):
    """A single motor designation parsed into components."""

    manufacturer: Optional[str] = None
    leading_number: Optional[str] = None  # CTI prefix e.g. "54"
    letter: str  # e.g. "M"
    number: str  # e.g. "2560"
    suffix: Optional[str] = None  # e.g. "WT", "-P", "/180"


class FlightCardExtraction(BaseModel):
    """Structured output schema for Qwen2.5-VL extraction."""

    flight_date_raw: Optional[str] = Field(
        None,
        description=(
            "The flight date exactly as written or circled on the card. "
            "May be a day-of-week name (e.g. 'Saturday') from a pre-printed list that was circled, "
            "a numeric date (e.g. '7/19'), or a full date. "
            "Treat a circled pre-printed day name the same as a handwritten day name."
        ),
    )
    flier_name: Optional[str] = None
    membership: Optional[MembershipInfo] = None
    rocket_name: Optional[str] = None
    rocket_manufacturer: Optional[str] = None
    rocket_colors: Optional[list[str]] = None
    measurements: Optional[RocketMeasurements] = None
    motors: Optional[list[list[MotorEntry]]] = Field(
        None,
        description="Outer list = stages (index 0 = stage 1). Inner list = motors in that stage.",
    )
    total_impulse_value: Optional[float] = None
    total_impulse_unit: Optional[str] = Field(None, description="'Ns' or 'LbsFt'")
    notes: Optional[str] = None
    flag_heads_up: Optional[bool] = None
    flag_first_flight: Optional[bool] = None
    flag_complex: Optional[bool] = None
    rack: Optional[str] = None
    pad: Optional[int] = None
    fso_rso_initials: Optional[str] = None
    evaluation_outcome: Optional[str] = Field(
        None,
        description=(
            "One of: good, motor, airframe, recovery. "
            "May be a circled pre-printed word on the card rather than handwritten text. "
            "Treat a circled pre-printed outcome word as the selected value."
        ),
    )
    evaluation_comments: Optional[str] = None


# ─── API Request/Response Schemas ─────────────────────────────────────────────


class ScanResponse(BaseModel):
    """Response returned after a card image is submitted."""

    record_id: int
    message: str = "Card received"


class SetModeRequest(BaseModel):
    """Request body for changing the extraction mode."""

    mode: str  # "immediate" | "deferred"


class ModeResponse(BaseModel):
    """Response confirming the current extraction mode."""

    mode: str
    message: str


class TriggerResponse(BaseModel):
    """Response after manually triggering extraction."""

    dispatched: int  # number of records enqueued


class RequeueResponse(BaseModel):
    """Response after requeuing failed records."""

    requeued: int  # number of records reset to pending


class FlightRecordSummary(BaseModel):
    """Summary view of a flight record for list endpoints."""

    id: int
    flier_name: Optional[str] = None
    rocket_name: Optional[str] = None  # from overflow
    motor_designation: Optional[str] = None  # human-readable, derived
    flight_date: Optional[date] = None
    created_at: datetime
    extraction_status: str


class FlightRecordDetail(BaseModel):
    """Full detail view of a flight record."""

    id: int
    image_url: str  # URL to static-served image
    extraction_status: str
    flight_date: Optional[date] = None
    flier_name: Optional[str] = None
    total_impulse_value: Optional[float] = None
    total_impulse_unit: Optional[str] = None
    flag_heads_up: Optional[bool] = None
    flag_first_flight: Optional[bool] = None
    flag_complex: Optional[bool] = None
    rack: Optional[str] = None
    pad: Optional[int] = None
    fso_rso_initials: Optional[str] = None
    evaluation_outcome: Optional[str] = None
    evaluation_comments: Optional[str] = None
    overflow: Optional[dict] = None
    created_at: datetime
