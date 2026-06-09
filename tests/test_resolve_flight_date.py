"""Unit tests for resolve_flight_date."""

from datetime import date

import pytest

from flight_card_scanner.config import DateRange
from flight_card_scanner.exceptions import DateResolutionError
from flight_card_scanner.services.extraction_service import resolve_flight_date


# A typical 3-day event: Fri Jul 18 – Sun Jul 20, 2025
RANGE_3DAY = DateRange(start=date(2025, 7, 18), end=date(2025, 7, 20))

# A 7-day event covering all weekdays: Mon Jul 14 – Sun Jul 20, 2025
RANGE_7DAY = DateRange(start=date(2025, 7, 14), end=date(2025, 7, 20))


class TestNoneAndEmpty:
    """None/empty inputs should return None."""

    def test_none_returns_none(self):
        assert resolve_flight_date(None, RANGE_3DAY) is None

    def test_empty_string_returns_none(self):
        assert resolve_flight_date("", RANGE_3DAY) is None

    def test_whitespace_only_returns_none(self):
        assert resolve_flight_date("   ", RANGE_3DAY) is None


class TestDayOfWeekResolution:
    """Day-of-week names resolve to the matching date in the range."""

    def test_full_name_saturday(self):
        result = resolve_flight_date("Saturday", RANGE_3DAY)
        assert result == date(2025, 7, 19)

    def test_full_name_friday(self):
        result = resolve_flight_date("Friday", RANGE_3DAY)
        assert result == date(2025, 7, 18)

    def test_full_name_sunday(self):
        result = resolve_flight_date("Sunday", RANGE_3DAY)
        assert result == date(2025, 7, 20)

    def test_abbreviated_sat(self):
        result = resolve_flight_date("Sat", RANGE_3DAY)
        assert result == date(2025, 7, 19)

    def test_abbreviated_fri(self):
        result = resolve_flight_date("fri", RANGE_3DAY)
        assert result == date(2025, 7, 18)

    def test_case_insensitive_uppercase(self):
        result = resolve_flight_date("SATURDAY", RANGE_3DAY)
        assert result == date(2025, 7, 19)

    def test_case_insensitive_mixed(self):
        result = resolve_flight_date("sAtUrDaY", RANGE_3DAY)
        assert result == date(2025, 7, 19)

    def test_with_leading_trailing_whitespace(self):
        result = resolve_flight_date("  Saturday  ", RANGE_3DAY)
        assert result == date(2025, 7, 19)

    def test_day_not_in_range_raises(self):
        # Monday is not in the 3-day range (Fri-Sun)
        with pytest.raises(DateResolutionError):
            resolve_flight_date("Monday", RANGE_3DAY)

    def test_all_days_resolvable_in_full_week(self):
        for day_name, expected_date in [
            ("Monday", date(2025, 7, 14)),
            ("Tuesday", date(2025, 7, 15)),
            ("Wednesday", date(2025, 7, 16)),
            ("Thursday", date(2025, 7, 17)),
            ("Friday", date(2025, 7, 18)),
            ("Saturday", date(2025, 7, 19)),
            ("Sunday", date(2025, 7, 20)),
        ]:
            assert resolve_flight_date(day_name, RANGE_7DAY) == expected_date


class TestISODateParsing:
    """ISO 8601 date strings are parsed and validated."""

    def test_iso_date_in_range(self):
        result = resolve_flight_date("2025-07-19", RANGE_3DAY)
        assert result == date(2025, 7, 19)

    def test_iso_date_at_start(self):
        result = resolve_flight_date("2025-07-18", RANGE_3DAY)
        assert result == date(2025, 7, 18)

    def test_iso_date_at_end(self):
        result = resolve_flight_date("2025-07-20", RANGE_3DAY)
        assert result == date(2025, 7, 20)

    def test_iso_date_out_of_range_raises(self):
        with pytest.raises(DateResolutionError):
            resolve_flight_date("2025-07-21", RANGE_3DAY)

    def test_iso_date_before_range_raises(self):
        with pytest.raises(DateResolutionError):
            resolve_flight_date("2025-07-17", RANGE_3DAY)


class TestUSDateFormats:
    """US-style M/D/YYYY and M/D/YY formats are parsed and validated."""

    def test_m_d_yyyy_in_range(self):
        result = resolve_flight_date("7/19/2025", RANGE_3DAY)
        assert result == date(2025, 7, 19)

    def test_m_d_yy_in_range(self):
        result = resolve_flight_date("7/19/25", RANGE_3DAY)
        assert result == date(2025, 7, 19)

    def test_m_d_yyyy_out_of_range_raises(self):
        with pytest.raises(DateResolutionError):
            resolve_flight_date("7/21/2025", RANGE_3DAY)

    def test_m_d_yy_out_of_range_raises(self):
        with pytest.raises(DateResolutionError):
            resolve_flight_date("7/21/25", RANGE_3DAY)


class TestMonthDayFormat:
    """M/D format (no year) assumes event start year."""

    def test_m_d_in_range(self):
        result = resolve_flight_date("7/19", RANGE_3DAY)
        assert result == date(2025, 7, 19)

    def test_m_d_out_of_range_raises(self):
        with pytest.raises(DateResolutionError):
            resolve_flight_date("7/21", RANGE_3DAY)

    def test_m_d_with_leading_zeros(self):
        result = resolve_flight_date("07/19", RANGE_3DAY)
        assert result == date(2025, 7, 19)


class TestContradictoryDayAndDate:
    """When a day-of-week and numeric date contradict each other, the day wins.

    Flight card volunteers often write "Friday" on a card and then add the
    date from memory. If the actual Friday in the event range is the 18th but
    they wrote "Friday 7/13", we trust the day name because it's more likely
    they got the number wrong than the day.
    """

    def test_day_wins_over_wrong_date_slash(self):
        # Fri Jul 18 is the real Friday; "Friday 7/13" has wrong number
        result = resolve_flight_date("Friday 7/13", RANGE_3DAY)
        assert result == date(2025, 7, 18)

    def test_day_wins_over_wrong_date_iso(self):
        # Sat Jul 19 is the real Saturday; "Saturday 2025-07-13" has wrong date
        result = resolve_flight_date("Saturday 2025-07-13", RANGE_3DAY)
        assert result == date(2025, 7, 19)

    def test_abbreviated_day_wins(self):
        # Sun Jul 20; "Sun 7/13" has wrong number
        result = resolve_flight_date("Sun 7/13", RANGE_3DAY)
        assert result == date(2025, 7, 20)

    def test_case_insensitive_day_wins(self):
        result = resolve_flight_date("FRIDAY 7/13", RANGE_3DAY)
        assert result == date(2025, 7, 18)

    def test_day_agrees_with_date_still_resolves_by_day(self):
        # Even when both agree, the resolution still works
        result = resolve_flight_date("Saturday 7/19", RANGE_3DAY)
        assert result == date(2025, 7, 19)

    def test_day_with_ordinal_suffix(self):
        # "Friday the 13th" style — day name is first word
        result = resolve_flight_date("Friday the 13th", RANGE_3DAY)
        assert result == date(2025, 7, 18)

    def test_day_not_in_range_with_contradictory_date_raises(self):
        # Monday is not in Fri-Sun range, even with a numeric date
        with pytest.raises(DateResolutionError):
            resolve_flight_date("Monday 7/18", RANGE_3DAY)

    def test_full_week_range_resolves_contradictory(self):
        # Wed Jul 16 is the real Wednesday; "Wednesday 7/20" has wrong day number
        result = resolve_flight_date("Wednesday 7/20", RANGE_7DAY)
        assert result == date(2025, 7, 16)


class TestUnresolvableValues:
    """Unrecognised strings raise DateResolutionError."""

    def test_gibberish_raises(self):
        with pytest.raises(DateResolutionError):
            resolve_flight_date("not-a-date", RANGE_3DAY)

    def test_partial_day_name_raises(self):
        with pytest.raises(DateResolutionError):
            resolve_flight_date("Satur", RANGE_3DAY)

    def test_invalid_format_raises(self):
        with pytest.raises(DateResolutionError):
            resolve_flight_date("July 19", RANGE_3DAY)


# ---------------------------------------------------------------------------
# Property-based tests (Hypothesis)
# ---------------------------------------------------------------------------

from datetime import timedelta

from hypothesis import given, settings, assume
import hypothesis.strategies as st

from flight_card_scanner.services.extraction_service import _DAY_NAMES


# All canonical day names (full + abbreviated)
_ALL_DAY_NAMES = [
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun",
]


def _apply_random_casing(name: str, draw) -> str:
    """Apply a random casing transformation to a day name."""
    case_style = draw(st.sampled_from(["lower", "upper", "title", "random"]))
    if case_style == "lower":
        return name.lower()
    elif case_style == "upper":
        return name.upper()
    elif case_style == "title":
        return name.title()
    else:
        # Random per-character casing
        chars = draw(
            st.tuples(*[st.booleans() for _ in name])
        )
        return "".join(
            c.upper() if flag else c.lower()
            for c, flag in zip(name, chars)
        )


@st.composite
def day_name_with_casing(draw):
    """Generate a day name with random casing."""
    name = draw(st.sampled_from(_ALL_DAY_NAMES))
    return _apply_random_casing(name, draw)


@st.composite
def day_name_and_range_containing_it(draw):
    """Generate a (day_name_with_casing, DateRange) tuple where the range is
    guaranteed to contain exactly one occurrence of the named weekday.

    Strategy:
    - Pick a day name (with random casing)
    - Pick a start date
    - Compute a range length between 1 and 6 days so there's exactly one
      occurrence of the target weekday in [start, start+length].
    """
    canonical_name = draw(st.sampled_from(_ALL_DAY_NAMES))
    cased_name = _apply_random_casing(canonical_name, draw)

    target_weekday = _DAY_NAMES[canonical_name.lower()]

    # Pick a start date in a reasonable range
    range_start = draw(st.dates(
        min_value=date(2000, 1, 1),
        max_value=date(2100, 12, 25),
    ))

    # Compute the offset from range_start to the first occurrence of target_weekday
    start_weekday = range_start.weekday()
    days_until_target = (target_weekday - start_weekday) % 7

    # The range must include exactly that one day. We need:
    #   range_length >= days_until_target (so the target day is included)
    #   range_length < days_until_target + 7 (so no second occurrence)
    # range_length is the number of days from start to end (inclusive range = end - start)
    min_length = days_until_target
    max_length = days_until_target + 6  # at most 6 more days after the target

    range_length = draw(st.integers(min_value=min_length, max_value=max_length))

    range_end = range_start + timedelta(days=range_length)

    # Guard against date overflow
    assume(range_end.year <= 9999)

    date_range = DateRange(start=range_start, end=range_end)

    # The expected resolved date
    expected_date = range_start + timedelta(days=days_until_target)

    return cased_name, date_range, expected_date


# Feature: flight-card-scanner, Property 8: Day-of-week date resolution
class TestDayOfWeekResolutionProperty:
    """Property-based test: day-of-week date resolution.

    **Validates: Requirements 5.10, 5.11**
    """

    @given(data=day_name_and_range_containing_it())
    @settings(max_examples=200)
    def test_resolves_to_unique_matching_date(self, data):
        """For every day name (full/abbreviated, any casing) and a date range
        containing that weekday, resolve_flight_date returns the unique
        matching calendar date."""
        day_name, date_range, expected_date = data

        result = resolve_flight_date(day_name, date_range)

        # The result must be the expected date
        assert result == expected_date
        # And that date's weekday must match the day name
        target_weekday = _DAY_NAMES[day_name.strip().lower()]
        assert result.weekday() == target_weekday
        # And the result must be within the range
        assert date_range.start <= result <= date_range.end
