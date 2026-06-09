"""Extraction queue management, worker pool, and Ollama dispatch.

The ExtractionService class (worker pool, queue, Ollama dispatch) will be
fully implemented in tasks 5.1 and 5.6.
"""

import re
from datetime import date, datetime, timedelta

from flight_card_scanner.config import DateRange
from flight_card_scanner.exceptions import DateResolutionError

# Day-of-week name mapping (full and abbreviated, lowercase) to Python weekday int
_DAY_NAMES: dict[str, int] = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

# Date formats to attempt, in priority order.
# The %m/%d format is handled separately to avoid Python 3.15 deprecation.
_DATE_FORMATS_WITH_YEAR = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y")


def resolve_flight_date(
    raw_value: str | None,
    date_range: DateRange,
) -> date | None:
    """Resolve a raw date string from the LLM to a calendar date.

    Cases handled:
    1. None / empty  → return None  (no date written on card)
    2. Day-of-week name (e.g. "Saturday", "Sat") →
         find the unique day within event_date_range that matches;
         if no match → raise DateResolutionError
    3. Numeric / ISO date string (e.g. "2025-07-19", "7/19/2025",
       "7/19/25", "7/19") →
         parse to a date; validate it falls within event_date_range;
         if out of range → raise DateResolutionError
    4. Unrecognised format → raise DateResolutionError

    Returns the resolved date, or None if raw_value is None/empty.
    Raises DateResolutionError if the value cannot be resolved to a date
    within the event date range.
    """
    if raw_value is None:
        return None

    stripped = raw_value.strip()
    if not stripped:
        return None

    normalized = stripped.lower()

    # --- Day-of-week resolution ---
    if normalized in _DAY_NAMES:
        target_weekday = _DAY_NAMES[normalized]
        current = date_range.start
        while current <= date_range.end:
            if current.weekday() == target_weekday:
                return current
            current += timedelta(days=1)
        raise DateResolutionError(
            f"Day-of-week '{raw_value}' does not occur within the event date range "
            f"({date_range.start} to {date_range.end})"
        )

    # --- Contradictory day+date combination ---
    # Handle cases like "Friday 7/13" or "Fri 2025-07-13" where the day name
    # and the numeric date disagree. The day-of-week is trusted over the number.
    # Pattern: optional day name prefix followed by a numeric date portion.
    contradiction_match = re.match(
        r"^([a-zA-Z]+)\s+(.+)$", stripped
    )
    if contradiction_match:
        day_part = contradiction_match.group(1).lower()
        date_part = contradiction_match.group(2).strip()
        if day_part in _DAY_NAMES:
            # We have a day name + something else — resolve by day name
            target_weekday = _DAY_NAMES[day_part]
            current = date_range.start
            while current <= date_range.end:
                if current.weekday() == target_weekday:
                    return current
                current += timedelta(days=1)
            raise DateResolutionError(
                f"Day-of-week '{contradiction_match.group(1)}' (from '{raw_value}') "
                f"does not occur within the event date range "
                f"({date_range.start} to {date_range.end})"
            )

    # --- Numeric / ISO date parsing ---
    for fmt in _DATE_FORMATS_WITH_YEAR:
        try:
            parsed = datetime.strptime(stripped, fmt).date()
        except ValueError:
            continue

        # Validate within range
        if date_range.start <= parsed <= date_range.end:
            return parsed
        raise DateResolutionError(
            f"Date '{raw_value}' (parsed as {parsed}) falls outside the event date range "
            f"({date_range.start} to {date_range.end})"
        )

    # Try M/D format (no year) — manually parse to avoid Python 3.15 deprecation
    md_match = re.match(r"^(\d{1,2})/(\d{1,2})$", stripped)
    if md_match:
        try:
            month = int(md_match.group(1))
            day = int(md_match.group(2))
            parsed = date(date_range.start.year, month, day)
        except ValueError:
            pass
        else:
            if date_range.start <= parsed <= date_range.end:
                return parsed
            raise DateResolutionError(
                f"Date '{raw_value}' (parsed as {parsed}) falls outside the event date range "
                f"({date_range.start} to {date_range.end})"
            )

    raise DateResolutionError(
        f"Cannot resolve '{raw_value}' to a valid date"
    )
