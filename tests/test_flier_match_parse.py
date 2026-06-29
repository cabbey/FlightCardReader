"""Tests for FlierMatchService internal methods: rapidfuzz-specific behavior.

Covers:
- _normalize_name(): whitespace stripping, lowercasing
- _compute_name_similarity(): score range and expected behavior
- _find_rows_by_member_number(): club specified (primary/fallback), no-club both columns
- _score_candidate(): name-only vs member-confirmed scoring
- Confidence tiering: auto-accept (>0.95) vs review (<=0.95)
"""

import tempfile
from pathlib import Path

import pytest

from flight_card_scanner.services.flier_match_service import FlierMatchService


@pytest.fixture
def service_with_rows() -> FlierMatchService:
    """Create a FlierMatchService with a loaded TSV containing 3 data rows."""
    tsv_content = (
        "Name\tNAR\tTRA\tLevel\n"
        "John Smith\t12345\t\t3\n"
        "Jane Doe\t\t54321\t2\n"
        "Bob Johnson\t67890\t11111\t1\n"
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".tsv", delete=False, encoding="utf-8"
    ) as f:
        f.write(tsv_content)
        path = Path(f.name)
    svc = FlierMatchService(known_fliers_path=path)
    svc.load()
    return svc


@pytest.fixture
def service_with_duplicates() -> FlierMatchService:
    """Service with duplicate member numbers across columns."""
    tsv_content = (
        "Name\tNAR\tTRA\tLevel\n"
        "Alice Brown\t99999\t\t2\n"
        "Charlie White\t\t99999\t3\n"
        "Dave Green\t99999\t88888\t1\n"
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".tsv", delete=False, encoding="utf-8"
    ) as f:
        f.write(tsv_content)
        path = Path(f.name)
    svc = FlierMatchService(known_fliers_path=path)
    svc.load()
    return svc


class TestNormalizeName:
    """Tests for _normalize_name() static method."""

    def test_strips_leading_whitespace(self) -> None:
        assert FlierMatchService._normalize_name("  John") == "john"

    def test_strips_trailing_whitespace(self) -> None:
        assert FlierMatchService._normalize_name("John  ") == "john"

    def test_strips_both_sides(self) -> None:
        assert FlierMatchService._normalize_name("  John Smith  ") == "john smith"

    def test_lowercases(self) -> None:
        assert FlierMatchService._normalize_name("JOHN SMITH") == "john smith"

    def test_mixed_case_and_whitespace(self) -> None:
        assert FlierMatchService._normalize_name("\t Jane DOE \n") == "jane doe"

    def test_empty_string(self) -> None:
        assert FlierMatchService._normalize_name("") == ""

    def test_already_normalized(self) -> None:
        assert FlierMatchService._normalize_name("bob") == "bob"


class TestComputeNameSimilarity:
    """Tests for _compute_name_similarity() using rapidfuzz WRatio."""

    def test_exact_match_high_score(self, service_with_rows: FlierMatchService) -> None:
        score = service_with_rows._compute_name_similarity("John Smith", "John Smith")
        assert score >= 95.0  # Exact match should be very close to 100

    def test_case_insensitive(self, service_with_rows: FlierMatchService) -> None:
        score = service_with_rows._compute_name_similarity("john smith", "JOHN SMITH")
        assert score >= 95.0

    def test_whitespace_insensitive(self, service_with_rows: FlierMatchService) -> None:
        score = service_with_rows._compute_name_similarity("  John Smith  ", "John Smith")
        assert score >= 95.0

    def test_partial_match_moderate_score(
        self, service_with_rows: FlierMatchService
    ) -> None:
        score = service_with_rows._compute_name_similarity("John", "John Smith")
        assert 40.0 < score < 100.0

    def test_totally_different_names_low_score(
        self, service_with_rows: FlierMatchService
    ) -> None:
        score = service_with_rows._compute_name_similarity("Xyz Abc", "John Smith")
        assert score < 50.0

    def test_score_in_valid_range(self, service_with_rows: FlierMatchService) -> None:
        score = service_with_rows._compute_name_similarity("Test Name", "Another Name")
        assert 0.0 <= score <= 100.0

    def test_token_reorder_handled(
        self, service_with_rows: FlierMatchService
    ) -> None:
        """WRatio handles token reordering (Smith John vs John Smith)."""
        score = service_with_rows._compute_name_similarity("Smith John", "John Smith")
        assert score >= 90.0


class TestFindRowsByMemberNumber:
    """Tests for _find_rows_by_member_number() three-case logic."""

    def test_club_specified_primary_match(
        self, service_with_rows: FlierMatchService
    ) -> None:
        """Club=NAR, number found in NAR column."""
        results = service_with_rows._find_rows_by_member_number("12345", "NAR")
        assert len(results) == 1
        idx, row = results[0]
        assert row["Name"] == "John Smith"
        assert idx == 0

    def test_club_specified_fallback_to_other_column(
        self, service_with_rows: FlierMatchService
    ) -> None:
        """Club=NAR specified but number only in TRA column - falls back."""
        results = service_with_rows._find_rows_by_member_number("54321", "NAR")
        assert len(results) == 1
        idx, row = results[0]
        assert row["Name"] == "Jane Doe"
        assert idx == 1

    def test_club_specified_tra_primary(
        self, service_with_rows: FlierMatchService
    ) -> None:
        """Club=TRA, number found in TRA column."""
        results = service_with_rows._find_rows_by_member_number("54321", "TRA")
        assert len(results) == 1
        idx, row = results[0]
        assert row["Name"] == "Jane Doe"

    def test_no_club_searches_both_columns(
        self, service_with_rows: FlierMatchService
    ) -> None:
        """No club specified - searches both NAR and TRA columns."""
        # 12345 is in NAR column
        results = service_with_rows._find_rows_by_member_number("12345", None)
        assert len(results) == 1
        idx, row = results[0]
        assert row["Name"] == "John Smith"

    def test_no_club_finds_tra_number(
        self, service_with_rows: FlierMatchService
    ) -> None:
        """No club specified - finds number in TRA column."""
        results = service_with_rows._find_rows_by_member_number("54321", None)
        assert len(results) == 1
        idx, row = results[0]
        assert row["Name"] == "Jane Doe"

    def test_no_club_finds_both_columns_simultaneously(
        self, service_with_duplicates: FlierMatchService
    ) -> None:
        """No club - number 99999 is in NAR for rows 0,2 and TRA for row 1."""
        results = service_with_duplicates._find_rows_by_member_number("99999", None)
        # Should find all three rows that have 99999 in either column
        assert len(results) == 3
        indices = {idx for idx, _ in results}
        assert indices == {0, 1, 2}

    def test_no_match_returns_empty(
        self, service_with_rows: FlierMatchService
    ) -> None:
        """Number not in any column returns empty list."""
        results = service_with_rows._find_rows_by_member_number("00000", "NAR")
        assert results == []

    def test_no_club_no_match_returns_empty(
        self, service_with_rows: FlierMatchService
    ) -> None:
        """No club, number not found anywhere."""
        results = service_with_rows._find_rows_by_member_number("00000", None)
        assert results == []

    def test_whitespace_in_number_stripped(
        self, service_with_rows: FlierMatchService
    ) -> None:
        """Member number with surrounding whitespace still matches."""
        results = service_with_rows._find_rows_by_member_number(" 12345 ", "NAR")
        assert len(results) == 1
        assert results[0][1]["Name"] == "John Smith"


class TestScoreCandidate:
    """Tests for _score_candidate() composite scoring."""

    def test_name_only_scoring(self, service_with_rows: FlierMatchService) -> None:
        """Without member confirmation, composite = name_sim, confidence = name_sim/100."""
        row = {"Name": "John Smith", "NAR": "12345", "TRA": "", "Level": "3"}
        composite, confidence = service_with_rows._score_candidate(
            "John Smith", row, member_confirmed=False
        )
        # Exact match: name_sim ~100, no bonus
        assert composite >= 95.0
        assert composite < 120.0  # No bonus added
        assert 0.95 <= confidence <= 1.0

    def test_member_confirmed_scoring(
        self, service_with_rows: FlierMatchService
    ) -> None:
        """With member confirmation, composite gets +20 bonus, confidence boosted."""
        row = {"Name": "John Smith", "NAR": "12345", "TRA": "", "Level": "3"}
        composite, confidence = service_with_rows._score_candidate(
            "John Smith", row, member_confirmed=True
        )
        # Exact match: name_sim ~100, +20 bonus
        assert composite >= 115.0
        assert confidence == 1.0  # min((~100 + 20) / 100, 1.0) = 1.0

    def test_member_confirmed_lower_name_sim(
        self, service_with_rows: FlierMatchService
    ) -> None:
        """Member confirmed with moderate name similarity still gets bonus."""
        row = {"Name": "John Smith", "NAR": "12345", "TRA": "", "Level": "3"}
        composite, confidence = service_with_rows._score_candidate(
            "J. Smith", row, member_confirmed=True
        )
        # Name sim will be moderate (not exact), but bonus added
        composite_no_member, conf_no_member = service_with_rows._score_candidate(
            "J. Smith", row, member_confirmed=False
        )
        assert composite > composite_no_member
        assert confidence > conf_no_member

    def test_confidence_clamped_to_one(
        self, service_with_rows: FlierMatchService
    ) -> None:
        """Confidence never exceeds 1.0 even with bonus."""
        row = {"Name": "Test Name", "NAR": "12345", "TRA": "", "Level": "3"}
        _, confidence = service_with_rows._score_candidate(
            "Test Name", row, member_confirmed=True
        )
        assert confidence <= 1.0

    def test_confidence_range_name_only(
        self, service_with_rows: FlierMatchService
    ) -> None:
        """Name-only confidence is always in [0.0, 1.0]."""
        row = {"Name": "Totally Different", "NAR": "", "TRA": "", "Level": "1"}
        _, confidence = service_with_rows._score_candidate(
            "Xyz Abc", row, member_confirmed=False
        )
        assert 0.0 <= confidence <= 1.0

    def test_member_bonus_is_exactly_20(
        self, service_with_rows: FlierMatchService
    ) -> None:
        """The member bonus adds exactly 20 to composite score."""
        row = {"Name": "John Smith", "NAR": "12345", "TRA": "", "Level": "3"}
        composite_confirmed, _ = service_with_rows._score_candidate(
            "John Smith", row, member_confirmed=True
        )
        composite_unconfirmed, _ = service_with_rows._score_candidate(
            "John Smith", row, member_confirmed=False
        )
        assert abs((composite_confirmed - composite_unconfirmed) - 20.0) < 0.01


class TestConfidenceTiering:
    """Test auto-accept vs review tiering by verifying confidence against threshold.

    The auto_accept_threshold is 0.95 (defined in ExtractionService/config).
    - confidence > 0.95: would be auto-accepted
    - confidence <= 0.95: would be flagged for review
    """

    AUTO_ACCEPT_THRESHOLD = 0.95

    def test_exact_match_member_confirmed_auto_accept(
        self, service_with_rows: FlierMatchService
    ) -> None:
        """Exact name + member confirmed -> confidence = 1.0 -> auto-accept."""
        row = {"Name": "John Smith", "NAR": "12345", "TRA": "", "Level": "3"}
        _, confidence = service_with_rows._score_candidate(
            "John Smith", row, member_confirmed=True
        )
        assert confidence > self.AUTO_ACCEPT_THRESHOLD

    def test_exact_match_name_only_at_boundary(
        self, service_with_rows: FlierMatchService
    ) -> None:
        """Exact name without member number -> confidence ~1.0 -> auto-accept."""
        row = {"Name": "John Smith", "NAR": "12345", "TRA": "", "Level": "3"}
        _, confidence = service_with_rows._score_candidate(
            "John Smith", row, member_confirmed=False
        )
        # name_sim/100 for exact match is ~1.0
        assert confidence > self.AUTO_ACCEPT_THRESHOLD

    def test_moderate_name_sim_member_confirmed_may_auto_accept(
        self, service_with_rows: FlierMatchService
    ) -> None:
        """Moderate name sim (~80) + member confirmed: confidence = min((80+20)/100, 1.0) = 1.0."""
        row = {"Name": "John Smith", "NAR": "12345", "TRA": "", "Level": "3"}
        _, confidence = service_with_rows._score_candidate(
            "Jon Smith", row, member_confirmed=True
        )
        # With member bonus, even moderate name sim can push above threshold
        # (80 + 20) / 100 = 1.0, which is > 0.95
        assert confidence > self.AUTO_ACCEPT_THRESHOLD

    def test_low_name_sim_name_only_review(
        self, service_with_rows: FlierMatchService
    ) -> None:
        """Low name similarity without member confirmation -> review tier."""
        row = {"Name": "John Smith", "NAR": "12345", "TRA": "", "Level": "3"}
        _, confidence = service_with_rows._score_candidate(
            "Robert Williams", row, member_confirmed=False
        )
        # Completely different name, sim will be very low
        assert confidence <= self.AUTO_ACCEPT_THRESHOLD

    def test_moderate_name_sim_name_only_review(
        self, service_with_rows: FlierMatchService
    ) -> None:
        """Moderate name sim (~80) without member confirmation -> confidence 0.80 -> review."""
        row = {"Name": "John Smith", "NAR": "12345", "TRA": "", "Level": "3"}
        _, confidence = service_with_rows._score_candidate(
            "John Smyth", row, member_confirmed=False
        )
        # Likely around 80-90 sim, so confidence 0.80-0.90, which is <= 0.95
        assert confidence <= self.AUTO_ACCEPT_THRESHOLD
