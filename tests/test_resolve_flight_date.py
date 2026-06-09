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
