"""Property-based test: Out-of-range date — full failure consequence chain.

# Feature: flight-card-scanner, Property 9: Out-of-range date — full failure consequence chain

Generates dates provably outside the event date range, runs through
resolve_flight_date (which raises DateResolutionError), then simulates the
caller's error handling via apply_extraction + set_status, and asserts:
  - flight_date = None
  - overflow['raw_flight_date'] = raw_string
  - extraction_status = "extraction_failed"

These three conditions must hold atomically after the consequence chain.

**Validates: Requirements 5.11, 5.12**
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings, assume
import hypothesis.strategies as st
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from flight_card_scanner.config import DateRange
from flight_card_scanner.database import Base
from flight_card_scanner.exceptions import DateResolutionError
from flight_card_scanner.models import FlightRecord
from flight_card_scanner.schemas import FlightCardExtraction
from flight_card_scanner.services.extraction_service import resolve_flight_date
from flight_card_scanner.services import record_service


# ---------------------------------------------------------------------------
# Async DB helper (used inside test body, not as a fixture)
# ---------------------------------------------------------------------------


async def _run_consequence_chain(raw_string: str, date_range: DateRange) -> None:
    """Run the full out-of-range date consequence chain against a fresh DB.

    Creates a record, attempts resolve_flight_date (expects DateResolutionError),
    handles the error via apply_extraction + set_status, then asserts the
    three atomic conditions.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with session_factory() as session:
            # 1. Create a flight record in 'pending' state
            record = FlightRecord(
                image_path="test/image.jpg",
                extraction_status="pending",
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            record_id = record.id

            # 2. Build an extraction with the out-of-range date
            extraction = FlightCardExtraction(flight_date_raw=raw_string)

            # 3. Attempt date resolution — must raise
            try:
                resolved_date = resolve_flight_date(raw_string, date_range)
                raise AssertionError(
                    f"Expected DateResolutionError but got resolved date: {resolved_date}"
                )
            except DateResolutionError:
                # 4. Handle as _process would:
                #    - apply_extraction with resolved_date=None
                #    - raw_flight_date ends up in overflow
                #    - then override status to extraction_failed
                await record_service.apply_extraction(
                    session, record_id, extraction, None
                )
                await record_service.set_status(
                    session, record_id, "extraction_failed"
                )

            # 5. Re-read the record and assert all three conditions
            await session.refresh(record)

            # flight_date must be None
            assert record.flight_date is None, (
                f"Expected flight_date=None but got {record.flight_date}"
            )

            # overflow must contain raw_flight_date = raw_string
            assert record.overflow is not None, "Expected overflow to be set"
            assert "raw_flight_date" in record.overflow, (
                f"Expected 'raw_flight_date' in overflow, got keys: "
                f"{list(record.overflow.keys())}"
            )
            assert record.overflow["raw_flight_date"] == raw_string, (
                f"Expected overflow['raw_flight_date'] = {raw_string!r}, "
                f"got {record.overflow['raw_flight_date']!r}"
            )

            # extraction_status must be "extraction_failed"
            assert record.extraction_status == "extraction_failed", (
                f"Expected extraction_status='extraction_failed', "
                f"got {record.extraction_status!r}"
            )

        await engine.dispose()


# ---------------------------------------------------------------------------
# Strategies for generating out-of-range dates
# ---------------------------------------------------------------------------


@st.composite
def out_of_range_iso_date(draw):
    """Generate an ISO date string provably outside an event date range.

    Strategy:
    - Pick a date range (3-14 days)
    - Pick a date that is before the start or after the end
    - Format as ISO string
    """
    # Generate a date range start
    range_start = draw(st.dates(
        min_value=date(2000, 1, 10),
        max_value=date(2100, 12, 20),
    ))
    # Range length 1-14 days
    range_length = draw(st.integers(min_value=1, max_value=14))
    range_end = range_start + timedelta(days=range_length)
    assume(range_end.year <= 9999)

    date_range = DateRange(start=range_start, end=range_end)

    # Generate a date outside the range: either before start or after end
    direction = draw(st.sampled_from(["before", "after"]))
    offset = draw(st.integers(min_value=1, max_value=365))

    if direction == "before":
        out_date = range_start - timedelta(days=offset)
        assume(out_date.year >= 1)
    else:
        out_date = range_end + timedelta(days=offset)
        assume(out_date.year <= 9999)

    raw_string = out_date.isoformat()  # e.g. "2025-07-21"
    return raw_string, date_range


@st.composite
def out_of_range_us_date(draw):
    """Generate a US-format date string (M/D/YYYY) provably outside a date range."""
    range_start = draw(st.dates(
        min_value=date(2000, 1, 10),
        max_value=date(2100, 12, 20),
    ))
    range_length = draw(st.integers(min_value=1, max_value=14))
    range_end = range_start + timedelta(days=range_length)
    assume(range_end.year <= 9999)

    date_range = DateRange(start=range_start, end=range_end)

    direction = draw(st.sampled_from(["before", "after"]))
    offset = draw(st.integers(min_value=1, max_value=365))

    if direction == "before":
        out_date = range_start - timedelta(days=offset)
        assume(out_date.year >= 1)
    else:
        out_date = range_end + timedelta(days=offset)
        assume(out_date.year <= 9999)

    # Format as M/D/YYYY
    raw_string = f"{out_date.month}/{out_date.day}/{out_date.year}"
    return raw_string, date_range


@st.composite
def out_of_range_day_name(draw):
    """Generate a day-of-week name not present in a short date range.

    Strategy:
    - Pick a date range of 1-5 days (so at least 2 weekdays are missing)
    - Pick a day name that does NOT occur in the range
    """
    range_start = draw(st.dates(
        min_value=date(2000, 1, 1),
        max_value=date(2100, 12, 25),
    ))
    # Short range: 1-5 days guarantees at least 2 missing weekdays
    range_length = draw(st.integers(min_value=1, max_value=5))
    range_end = range_start + timedelta(days=range_length)
    assume(range_end.year <= 9999)

    date_range = DateRange(start=range_start, end=range_end)

    # Find weekdays present in the range
    present_weekdays = set()
    current = range_start
    while current <= range_end:
        present_weekdays.add(current.weekday())
        current += timedelta(days=1)

    # All weekday indices
    all_weekdays = set(range(7))
    missing_weekdays = all_weekdays - present_weekdays
    assume(len(missing_weekdays) > 0)

    # Map weekday int to day names
    day_names_full = ["Monday", "Tuesday", "Wednesday", "Thursday",
                      "Friday", "Saturday", "Sunday"]
    day_names_abbr = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    missing_weekday = draw(st.sampled_from(sorted(missing_weekdays)))

    # Choose full or abbreviated name, with random casing
    use_abbr = draw(st.booleans())
    if use_abbr:
        name = day_names_abbr[missing_weekday]
    else:
        name = day_names_full[missing_weekday]

    # Apply random casing
    case_style = draw(st.sampled_from(["lower", "upper", "title", "original"]))
    if case_style == "lower":
        raw_string = name.lower()
    elif case_style == "upper":
        raw_string = name.upper()
    elif case_style == "title":
        raw_string = name.title()
    else:
        raw_string = name

    return raw_string, date_range


# Combine all out-of-range strategies
out_of_range_date_strategy = st.one_of(
    out_of_range_iso_date(),
    out_of_range_us_date(),
    out_of_range_day_name(),
)


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


# Feature: flight-card-scanner, Property 9: Out-of-range date — full failure consequence chain
class TestOutOfRangeDateConsequenceChain:
    """Property 9: Out-of-range date — full failure consequence chain.

    For dates provably outside the event date range, the full pipeline
    (resolve_flight_date raising DateResolutionError, then the caller handling
    it) must result in:
      - flight_date = None
      - overflow['raw_flight_date'] = raw_string
      - extraction_status = "extraction_failed"

    **Validates: Requirements 5.11, 5.12**
    """

    @given(data=out_of_range_date_strategy)
    @settings(max_examples=100)
    def test_resolve_flight_date_raises_for_out_of_range(self, data):
        """resolve_flight_date raises DateResolutionError for all out-of-range dates."""
        raw_string, date_range = data

        with pytest.raises(DateResolutionError):
            resolve_flight_date(raw_string, date_range)

    @given(data=out_of_range_date_strategy)
    @settings(max_examples=50)
    def test_full_consequence_chain(self, data):
        """The full failure consequence chain holds atomically:
        flight_date=None, overflow contains raw_flight_date, status=extraction_failed.

        Simulates what _process does:
        1. Create a record (pending)
        2. Attempt resolve_flight_date → raises DateResolutionError
        3. Handle the error: apply extraction with None date, store raw in overflow,
           set status to extraction_failed
        """
        raw_string, date_range = data
        asyncio.run(_run_consequence_chain(raw_string, date_range))
