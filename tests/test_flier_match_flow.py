"""Tests for FlierMatchService.match_flier() async method (rapidfuzz-based)."""

import tempfile
from pathlib import Path

import pytest

from flight_card_scanner.services.flier_match_service import (
    FlierMatchResult,
    FlierMatchService,
)


@pytest.fixture
def tsv_path() -> Path:
    """Create a temporary TSV file with test roster data."""
    tsv_content = (
        "Name\tNAR\tTRA\tLevel\n"
        "John Smith\t12345\t\t3\n"
        "Jane Doe\t\t54321\t2\n"
        "Bob Johnson\t67890\t11111\t1\n"
        "Robert Smithson\t99999\t\t2\n"
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".tsv", delete=False, encoding="utf-8"
    ) as f:
        f.write(tsv_content)
        return Path(f.name)


@pytest.fixture
def service(tsv_path: Path) -> FlierMatchService:
    """Create a FlierMatchService with a loaded TSV containing 4 data rows."""
    svc = FlierMatchService(known_fliers_path=tsv_path)
    svc.load()
    return svc


@pytest.fixture
def disabled_service(tmp_path: Path) -> FlierMatchService:
    """Create a FlierMatchService with an empty TSV (disabled)."""
    empty_tsv = tmp_path / "empty.tsv"
    empty_tsv.write_text("Name\tNAR\tTRA\tLevel\n", encoding="utf-8")
    svc = FlierMatchService(known_fliers_path=empty_tsv)
    svc.load()
    return svc


class TestExactNameMatch:
    """Tests for exact name matches against the roster."""

    @pytest.mark.anyio
    async def test_exact_name_match_returns_matched(
        self, service: FlierMatchService
    ) -> None:
        """An exact name match should return matched=True with correct row data."""
        result = await service.match_flier(
            flier_name="John Smith",
            club="NAR",
            member_number="12345",
            cert_level=3,
        )

        assert result.matched is True
        assert result.row_data is not None
        assert result.row_data["Name"] == "John Smith"
        assert result.row_data["NAR"] == "12345"
        assert result.error is None

    @pytest.mark.anyio
    async def test_exact_name_match_high_confidence(
        self, service: FlierMatchService
    ) -> None:
        """An exact name + member number match should produce high confidence."""
        result = await service.match_flier(
            flier_name="John Smith",
            club="NAR",
            member_number="12345",
            cert_level=3,
        )

        assert result.matched is True
        # Exact name (score ~100) + member confirmed → confidence = min((100+20)/100, 1.0) = 1.0
        assert result.confidence >= 0.95
        assert result.confidence <= 1.0

    @pytest.mark.anyio
    async def test_exact_name_without_member_number(
        self, service: FlierMatchService
    ) -> None:
        """An exact name match without member number uses name-only scoring."""
        result = await service.match_flier(
            flier_name="Jane Doe",
            club=None,
            member_number=None,
            cert_level=None,
        )

        assert result.matched is True
        assert result.row_data is not None
        assert result.row_data["Name"] == "Jane Doe"
        # Name-only confidence = name_similarity / 100.0 (exact → ~1.0)
        assert result.confidence >= 0.9
        assert result.confidence <= 1.0


class TestCloseNameMatch:
    """Tests for close but imperfect name matches."""

    @pytest.mark.anyio
    async def test_close_name_matches_above_threshold(
        self, service: FlierMatchService
    ) -> None:
        """A close name variant (e.g., misspelling) should still match if above threshold."""
        result = await service.match_flier(
            flier_name="Jon Smith",
            club=None,
            member_number=None,
            cert_level=None,
        )

        assert result.matched is True
        assert result.row_data is not None
        assert result.row_data["Name"] == "John Smith"
        # Close but not exact → confidence should be high but < 1.0
        assert 0.0 < result.confidence <= 1.0

    @pytest.mark.anyio
    async def test_name_reordering_matches(
        self, service: FlierMatchService
    ) -> None:
        """WRatio handles token reordering (last, first vs first last)."""
        result = await service.match_flier(
            flier_name="Doe Jane",
            club=None,
            member_number=None,
            cert_level=None,
        )

        assert result.matched is True
        assert result.row_data is not None
        assert result.row_data["Name"] == "Jane Doe"


class TestMemberNumberConfirmation:
    """Tests for member number confirmation with lower name similarity."""

    @pytest.mark.anyio
    async def test_member_number_lowers_threshold(
        self, service: FlierMatchService
    ) -> None:
        """Member number confirmation allows a lower name similarity to still match.

        Default thresholds: name_only=80, member_confirmed=60.
        A name with similarity between 60-80 should match only when member number confirms.
        """
        # "Bob J" is a shortened version of "Bob Johnson" — similarity typically
        # around 70-80 with WRatio. With member confirmation the threshold is 60.
        result = await service.match_flier(
            flier_name="Bob J",
            club="NAR",
            member_number="67890",
            cert_level=None,
        )

        assert result.matched is True
        assert result.row_data is not None
        assert result.row_data["Name"] == "Bob Johnson"
        # Member-confirmed confidence = min((name_sim + 20) / 100, 1.0)
        assert 0.0 < result.confidence <= 1.0

    @pytest.mark.anyio
    async def test_member_confirmed_confidence_boosted(
        self, service: FlierMatchService
    ) -> None:
        """Member-confirmed matches get a +20 boost in confidence calculation."""
        # Get name-only result
        name_only_result = await service.match_flier(
            flier_name="John Smith",
            club=None,
            member_number=None,
            cert_level=None,
        )

        # Get member-confirmed result for same name
        member_result = await service.match_flier(
            flier_name="John Smith",
            club="NAR",
            member_number="12345",
            cert_level=None,
        )

        # Member-confirmed should have equal or higher confidence
        assert member_result.confidence >= name_only_result.confidence


class TestNoClubWithNumber:
    """Tests for the no-club-with-number scenario (member number without club hint)."""

    @pytest.mark.anyio
    async def test_member_number_found_in_nar_without_club(
        self, service: FlierMatchService
    ) -> None:
        """Member number found in NAR column without specifying club."""
        result = await service.match_flier(
            flier_name="John Smith",
            club=None,
            member_number="12345",
            cert_level=None,
        )

        assert result.matched is True
        assert result.row_data is not None
        assert result.row_data["Name"] == "John Smith"
        assert result.row_data["NAR"] == "12345"

    @pytest.mark.anyio
    async def test_member_number_found_in_tra_without_club(
        self, service: FlierMatchService
    ) -> None:
        """Member number found in TRA column without specifying club."""
        result = await service.match_flier(
            flier_name="Jane Doe",
            club=None,
            member_number="54321",
            cert_level=None,
        )

        assert result.matched is True
        assert result.row_data is not None
        assert result.row_data["Name"] == "Jane Doe"
        assert result.row_data["TRA"] == "54321"

    @pytest.mark.anyio
    async def test_member_number_in_both_columns_picks_best_name(
        self, service: FlierMatchService
    ) -> None:
        """When member number exists in a row with both columns populated, still works."""
        # Bob Johnson has both NAR=67890 and TRA=11111
        result = await service.match_flier(
            flier_name="Bob Johnson",
            club=None,
            member_number="67890",
            cert_level=None,
        )

        assert result.matched is True
        assert result.row_data is not None
        assert result.row_data["Name"] == "Bob Johnson"


class TestBelowThresholdNoMatch:
    """Tests for scenarios where no match should be returned."""

    @pytest.mark.anyio
    async def test_completely_different_name_no_match(
        self, service: FlierMatchService
    ) -> None:
        """A completely unrelated name should not match any roster entry."""
        result = await service.match_flier(
            flier_name="Xyzzy Plugh",
            club=None,
            member_number=None,
            cert_level=None,
        )

        assert result.matched is False
        assert result.line_number == 0
        assert result.row_data is None
        assert result.confidence == 0.0
        assert result.error is None

    @pytest.mark.anyio
    async def test_none_flier_name_no_match(
        self, service: FlierMatchService
    ) -> None:
        """None flier_name should immediately return no match."""
        result = await service.match_flier(
            flier_name=None,
            club=None,
            member_number=None,
            cert_level=None,
        )

        assert result.matched is False
        assert result.line_number == 0
        assert result.row_data is None
        assert result.confidence == 0.0

    @pytest.mark.anyio
    async def test_empty_flier_name_no_match(
        self, service: FlierMatchService
    ) -> None:
        """Empty string flier_name should return no match."""
        result = await service.match_flier(
            flier_name="",
            club=None,
            member_number=None,
            cert_level=None,
        )

        assert result.matched is False
        assert result.confidence == 0.0


class TestDisabledService:
    """Tests for when the service is disabled (empty roster)."""

    @pytest.mark.anyio
    async def test_disabled_service_returns_no_match(
        self, disabled_service: FlierMatchService
    ) -> None:
        """A disabled service (no data rows) should return no match."""
        assert disabled_service.enabled is False

        result = await disabled_service.match_flier(
            flier_name="John Smith",
            club="NAR",
            member_number="12345",
            cert_level=3,
        )

        assert result.matched is False
        assert result.line_number == 0
        assert result.row_data is None
        assert result.confidence == 0.0
        assert result.error is None


class TestConfidenceScores:
    """Tests verifying confidence score ranges and tiering."""

    @pytest.mark.anyio
    async def test_confidence_in_valid_range(
        self, service: FlierMatchService
    ) -> None:
        """All confidence scores must be in [0.0, 1.0]."""
        # Test with a match
        result = await service.match_flier(
            flier_name="John Smith",
            club="NAR",
            member_number="12345",
            cert_level=3,
        )
        assert 0.0 <= result.confidence <= 1.0

        # Test with no match
        result_no_match = await service.match_flier(
            flier_name="Xyzzy Plugh",
            club=None,
            member_number=None,
            cert_level=None,
        )
        assert result_no_match.confidence == 0.0

    @pytest.mark.anyio
    async def test_matched_true_implies_positive_confidence(
        self, service: FlierMatchService
    ) -> None:
        """When matched=True, confidence must be > 0.0."""
        result = await service.match_flier(
            flier_name="Jane Doe",
            club=None,
            member_number=None,
            cert_level=None,
        )

        assert result.matched is True
        assert result.confidence > 0.0

    @pytest.mark.anyio
    async def test_matched_false_implies_zero_confidence(
        self, service: FlierMatchService
    ) -> None:
        """When matched=False, confidence must be exactly 0.0."""
        result = await service.match_flier(
            flier_name="Xyzzy Plugh",
            club=None,
            member_number=None,
            cert_level=None,
        )

        assert result.matched is False
        assert result.confidence == 0.0

    @pytest.mark.anyio
    async def test_member_confirmed_higher_confidence_than_name_only_at_same_similarity(
        self, service: FlierMatchService
    ) -> None:
        """For the same name, member-confirmed should produce >= confidence than name-only."""
        # Same name, with and without member number
        result_name_only = await service.match_flier(
            flier_name="Bob Johnson",
            club=None,
            member_number=None,
            cert_level=None,
        )

        result_member = await service.match_flier(
            flier_name="Bob Johnson",
            club="NAR",
            member_number="67890",
            cert_level=None,
        )

        assert result_member.confidence >= result_name_only.confidence
