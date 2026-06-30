"""Integration tests for ExtractionService._apply_flier_match() tiered logic.

Tests the three-way branching:
1. Error/not_found → store status in overflow only
2. High confidence (> 0.95) → auto-accept, set flier_verified=True, import roster data
3. Lower confidence (≤ 0.95) → review, set flier_verified=False, import roster data

Both matched tiers apply the SAME data import: roster name replaces flier_name,
both NAR/TRA numbers are stored, cert_level is stored, and confidence is recorded.
The only difference is flier_verified and flier_match_status.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from flight_card_scanner.config import AppConfig
from flight_card_scanner.services.extraction_service import ExtractionService
from flight_card_scanner.services.flier_match_service import FlierMatchResult

# Patch target: the lazy import inside _apply_flier_match does
# "from flight_card_scanner.services import record_service"
# so we patch record_service.get at the package level.
_PATCH_RS_GET = "flight_card_scanner.services.record_service.get"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def minimal_config() -> AppConfig:
    """Create a minimal AppConfig with auto_accept_threshold = 0.95."""
    return AppConfig(auto_accept_threshold=0.95)


@pytest.fixture
def extraction_service(minimal_config: AppConfig) -> ExtractionService:
    """Create an ExtractionService with mocked dependencies."""
    session_factory = AsyncMock()
    return ExtractionService(
        config=minimal_config,
        session_factory=session_factory,
    )


def _make_record(
    record_id: int = 1,
    flier_name: str | None = "Original Name",
    flier_verified: bool = False,
    overflow: dict | None = None,
) -> MagicMock:
    """Create a mock FlightRecord with the given attributes."""
    record = MagicMock()
    record.id = record_id
    record.flier_name = flier_name
    record.flier_verified = flier_verified
    record.overflow = overflow
    return record


# ---------------------------------------------------------------------------
# Tests: High-confidence auto-accept path
# ---------------------------------------------------------------------------


class TestHighConfidenceAutoAccept:
    """High confidence (> 0.95) triggers auto-accept path."""

    @pytest.mark.anyio
    async def test_sets_flier_verified_true(
        self, extraction_service: ExtractionService
    ) -> None:
        """Auto-accept sets flier_verified=True on the record."""
        record = _make_record(flier_name="Original Name", overflow={})
        db = AsyncMock()

        result = FlierMatchResult(
            matched=True,
            line_number=3,
            row_data={"Name": "John Smith", "NAR": "12345", "TRA": "", "Level": "3"},
            error=None,
            confidence=0.98,
        )

        with patch(_PATCH_RS_GET, new=AsyncMock(return_value=record)):
            await extraction_service._apply_flier_match(db, 1, result)

        assert record.flier_verified is True

    @pytest.mark.anyio
    async def test_sets_status_verified(
        self, extraction_service: ExtractionService
    ) -> None:
        """Auto-accept sets flier_match_status to 'verified' in overflow."""
        record = _make_record(flier_name="Original Name", overflow={})
        db = AsyncMock()

        result = FlierMatchResult(
            matched=True,
            line_number=3,
            row_data={"Name": "John Smith", "NAR": "12345", "TRA": "", "Level": "3"},
            error=None,
            confidence=0.98,
        )

        with patch(_PATCH_RS_GET, new=AsyncMock(return_value=record)):
            await extraction_service._apply_flier_match(db, 1, result)

        assert record.overflow["flier_match_status"] == "verified"

    @pytest.mark.anyio
    async def test_applies_row_data_to_record(
        self, extraction_service: ExtractionService
    ) -> None:
        """Auto-accept applies matched row name and membership to the record."""
        record = _make_record(
            flier_name="Original Name",
            overflow={"membership": {"club": "NAR", "member_number": "00000"}},
        )
        db = AsyncMock()

        result = FlierMatchResult(
            matched=True,
            line_number=5,
            row_data={"Name": "John Smith", "NAR": "12345", "TRA": "", "Level": "2"},
            error=None,
            confidence=0.97,
        )

        with patch(_PATCH_RS_GET, new=AsyncMock(return_value=record)):
            await extraction_service._apply_flier_match(db, 1, result)

        # Name is overwritten with matched row data
        assert record.flier_name == "John Smith"
        # Membership uses unified format with both club numbers
        assert record.overflow["membership"]["nar_number"] == "12345"
        assert record.overflow["membership"]["tra_number"] is None
        assert record.overflow["membership"]["cert_level"] == 2

    @pytest.mark.anyio
    async def test_stores_confidence_in_overflow(
        self, extraction_service: ExtractionService
    ) -> None:
        """Auto-accept stores the confidence value in overflow."""
        record = _make_record(overflow={})
        db = AsyncMock()

        result = FlierMatchResult(
            matched=True,
            line_number=3,
            row_data={"Name": "John Smith", "NAR": "12345", "TRA": "", "Level": "3"},
            error=None,
            confidence=0.98,
        )

        with patch(_PATCH_RS_GET, new=AsyncMock(return_value=record)):
            await extraction_service._apply_flier_match(db, 1, result)

        assert record.overflow["flier_match_confidence"] == 0.98

    @pytest.mark.anyio
    async def test_commits_db_session(
        self, extraction_service: ExtractionService
    ) -> None:
        """Auto-accept commits the database session."""
        record = _make_record(overflow={})
        db = AsyncMock()

        result = FlierMatchResult(
            matched=True,
            line_number=3,
            row_data={"Name": "John Smith", "NAR": "12345", "TRA": "", "Level": "3"},
            error=None,
            confidence=0.98,
        )

        with patch(_PATCH_RS_GET, new=AsyncMock(return_value=record)):
            await extraction_service._apply_flier_match(db, 1, result)

        db.commit.assert_awaited_once()

    @pytest.mark.anyio
    async def test_tra_membership_applied(
        self, extraction_service: ExtractionService
    ) -> None:
        """Auto-accept correctly applies TRA membership from row data."""
        record = _make_record(overflow={})
        db = AsyncMock()

        result = FlierMatchResult(
            matched=True,
            line_number=4,
            row_data={"Name": "Jane Doe", "NAR": "", "TRA": "54321", "Level": "1"},
            error=None,
            confidence=0.99,
        )

        with patch(_PATCH_RS_GET, new=AsyncMock(return_value=record)):
            await extraction_service._apply_flier_match(db, 1, result)

        # Unified format stores both club numbers
        assert record.overflow["membership"]["nar_number"] is None
        assert record.overflow["membership"]["tra_number"] == "54321"
        assert record.overflow["membership"]["cert_level"] == 1


# ---------------------------------------------------------------------------
# Tests: Lower-confidence review path
# ---------------------------------------------------------------------------


class TestLowerConfidenceReview:
    """Lower confidence (≤ 0.95) triggers review path."""

    @pytest.mark.anyio
    async def test_sets_flier_verified_false(
        self, extraction_service: ExtractionService
    ) -> None:
        """Review path sets flier_verified=False on the record."""
        record = _make_record(flier_name="Original Name", overflow={})
        db = AsyncMock()

        result = FlierMatchResult(
            matched=True,
            line_number=3,
            row_data={"Name": "John Smith", "NAR": "12345", "TRA": "", "Level": "3"},
            error=None,
            confidence=0.85,
        )

        with patch(_PATCH_RS_GET, new=AsyncMock(return_value=record)):
            await extraction_service._apply_flier_match(db, 1, result)

        assert record.flier_verified is False

    @pytest.mark.anyio
    async def test_sets_status_review(
        self, extraction_service: ExtractionService
    ) -> None:
        """Review path sets flier_match_status to 'review' in overflow."""
        record = _make_record(flier_name="Original Name", overflow={})
        db = AsyncMock()

        result = FlierMatchResult(
            matched=True,
            line_number=3,
            row_data={"Name": "John Smith", "NAR": "12345", "TRA": "", "Level": "3"},
            error=None,
            confidence=0.85,
        )

        with patch(_PATCH_RS_GET, new=AsyncMock(return_value=record)):
            await extraction_service._apply_flier_match(db, 1, result)

        assert record.overflow["flier_match_status"] == "review"

    @pytest.mark.anyio
    async def test_applies_roster_data_same_as_high_confidence(
        self, extraction_service: ExtractionService
    ) -> None:
        """Review path applies the same roster data import as high-confidence tier."""
        record = _make_record(flier_name="Original Name", overflow={})
        db = AsyncMock()

        result = FlierMatchResult(
            matched=True,
            line_number=7,
            row_data={"Name": "John Smith", "NAR": "12345", "TRA": "", "Level": "3"},
            error=None,
            confidence=0.82,
        )

        with patch(_PATCH_RS_GET, new=AsyncMock(return_value=record)):
            await extraction_service._apply_flier_match(db, 1, result)

        # Roster name applied to record
        assert record.flier_name == "John Smith"
        # Membership uses unified format
        assert record.overflow["membership"]["nar_number"] == "12345"
        assert record.overflow["membership"]["tra_number"] is None
        assert record.overflow["membership"]["cert_level"] == 3
        # Confidence stored
        assert record.overflow["flier_match_confidence"] == 0.82
        # No flier_match_candidate key (removed)
        assert "flier_match_candidate" not in record.overflow

    @pytest.mark.anyio
    async def test_overwrites_flier_name_with_roster_name(
        self, extraction_service: ExtractionService
    ) -> None:
        """Review path DOES overwrite flier_name with the roster name (unified behavior)."""
        record = _make_record(flier_name="Original Name", overflow={})
        db = AsyncMock()

        result = FlierMatchResult(
            matched=True,
            line_number=3,
            row_data={"Name": "Different Name", "NAR": "12345", "TRA": "", "Level": "3"},
            error=None,
            confidence=0.85,
        )

        with patch(_PATCH_RS_GET, new=AsyncMock(return_value=record)):
            await extraction_service._apply_flier_match(db, 1, result)

        # Both tiers now apply the roster name
        assert record.flier_name == "Different Name"

    @pytest.mark.anyio
    async def test_overwrites_existing_membership_with_roster_data(
        self, extraction_service: ExtractionService
    ) -> None:
        """Review path DOES overwrite membership with roster data (unified behavior)."""
        existing_membership = {"club": "TRA", "member_number": "99999"}
        record = _make_record(
            flier_name="Original Name",
            overflow={"membership": existing_membership},
        )
        db = AsyncMock()

        result = FlierMatchResult(
            matched=True,
            line_number=3,
            row_data={"Name": "John Smith", "NAR": "12345", "TRA": "", "Level": "3"},
            error=None,
            confidence=0.90,
        )

        with patch(_PATCH_RS_GET, new=AsyncMock(return_value=record)):
            await extraction_service._apply_flier_match(db, 1, result)

        # Both tiers now overwrite membership with unified roster data
        # Includes standard fields (club, member_number) for template compatibility
        assert record.overflow["membership"] == {
            "nar_number": "12345",
            "tra_number": None,
            "club": "NAR",
            "member_number": "12345",
            "cert_level": 3,
        }

    @pytest.mark.anyio
    async def test_stores_confidence_in_overflow(
        self, extraction_service: ExtractionService
    ) -> None:
        """Review path stores the confidence value in overflow."""
        record = _make_record(overflow={})
        db = AsyncMock()

        result = FlierMatchResult(
            matched=True,
            line_number=3,
            row_data={"Name": "John Smith", "NAR": "12345", "TRA": "", "Level": "3"},
            error=None,
            confidence=0.88,
        )

        with patch(_PATCH_RS_GET, new=AsyncMock(return_value=record)):
            await extraction_service._apply_flier_match(db, 1, result)

        assert record.overflow["flier_match_confidence"] == 0.88

    @pytest.mark.anyio
    async def test_boundary_confidence_at_threshold(
        self, extraction_service: ExtractionService
    ) -> None:
        """Confidence exactly at threshold (0.95) triggers review, not auto-accept."""
        record = _make_record(flier_name="Original Name", overflow={})
        db = AsyncMock()

        result = FlierMatchResult(
            matched=True,
            line_number=3,
            row_data={"Name": "John Smith", "NAR": "12345", "TRA": "", "Level": "3"},
            error=None,
            confidence=0.95,  # exactly at threshold
        )

        with patch(_PATCH_RS_GET, new=AsyncMock(return_value=record)):
            await extraction_service._apply_flier_match(db, 1, result)

        # 0.95 is NOT > 0.95, so review path
        assert record.overflow["flier_match_status"] == "review"
        assert record.flier_verified is False
        # But both tiers apply roster name (unified behavior)
        assert record.flier_name == "John Smith"


# ---------------------------------------------------------------------------
# Tests: Error result
# ---------------------------------------------------------------------------


class TestErrorResult:
    """Error result stores 'error' status in overflow."""

    @pytest.mark.anyio
    async def test_sets_error_status(
        self, extraction_service: ExtractionService
    ) -> None:
        """Error result sets flier_match_status to 'error' in overflow."""
        record = _make_record(overflow={})
        db = AsyncMock()

        result = FlierMatchResult(
            matched=False,
            line_number=0,
            row_data=None,
            error="Something went wrong",
            confidence=0.0,
        )

        with patch(_PATCH_RS_GET, new=AsyncMock(return_value=record)):
            await extraction_service._apply_flier_match(db, 1, result)

        assert record.overflow["flier_match_status"] == "error"
        assert record.overflow["flier_match_error"] == "Something went wrong"

    @pytest.mark.anyio
    async def test_error_does_not_set_verified(
        self, extraction_service: ExtractionService
    ) -> None:
        """Error result does not modify flier_verified."""
        record = _make_record(flier_verified=False, overflow={})
        db = AsyncMock()

        result = FlierMatchResult(
            matched=False,
            line_number=0,
            row_data=None,
            error="Connection timeout",
            confidence=0.0,
        )

        with patch(_PATCH_RS_GET, new=AsyncMock(return_value=record)):
            await extraction_service._apply_flier_match(db, 1, result)

        assert record.flier_verified is False

    @pytest.mark.anyio
    async def test_error_commits_db(
        self, extraction_service: ExtractionService
    ) -> None:
        """Error result commits the database session."""
        record = _make_record(overflow={})
        db = AsyncMock()

        result = FlierMatchResult(
            matched=False,
            line_number=0,
            row_data=None,
            error="Network error",
            confidence=0.0,
        )

        with patch(_PATCH_RS_GET, new=AsyncMock(return_value=record)):
            await extraction_service._apply_flier_match(db, 1, result)

        db.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Tests: Not found result
# ---------------------------------------------------------------------------


class TestNotFoundResult:
    """Not found result stores 'not_found' status in overflow."""

    @pytest.mark.anyio
    async def test_sets_not_found_status(
        self, extraction_service: ExtractionService
    ) -> None:
        """Not found result sets flier_match_status to 'not_found' in overflow."""
        record = _make_record(overflow={})
        db = AsyncMock()

        result = FlierMatchResult(
            matched=False,
            line_number=0,
            row_data=None,
            error=None,
            confidence=0.0,
        )

        with patch(_PATCH_RS_GET, new=AsyncMock(return_value=record)):
            await extraction_service._apply_flier_match(db, 1, result)

        assert record.overflow["flier_match_status"] == "not_found"

    @pytest.mark.anyio
    async def test_not_found_does_not_set_confidence(
        self, extraction_service: ExtractionService
    ) -> None:
        """Not found result does not store confidence in overflow."""
        record = _make_record(overflow={})
        db = AsyncMock()

        result = FlierMatchResult(
            matched=False,
            line_number=0,
            row_data=None,
            error=None,
            confidence=0.0,
        )

        with patch(_PATCH_RS_GET, new=AsyncMock(return_value=record)):
            await extraction_service._apply_flier_match(db, 1, result)

        assert "flier_match_confidence" not in record.overflow

    @pytest.mark.anyio
    async def test_not_found_does_not_modify_flier_name(
        self, extraction_service: ExtractionService
    ) -> None:
        """Not found result does not modify the record's flier_name."""
        record = _make_record(flier_name="Existing Flier", overflow={})
        db = AsyncMock()

        result = FlierMatchResult(
            matched=False,
            line_number=0,
            row_data=None,
            error=None,
            confidence=0.0,
        )

        with patch(_PATCH_RS_GET, new=AsyncMock(return_value=record)):
            await extraction_service._apply_flier_match(db, 1, result)

        assert record.flier_name == "Existing Flier"

    @pytest.mark.anyio
    async def test_not_found_commits_db(
        self, extraction_service: ExtractionService
    ) -> None:
        """Not found result commits the database session."""
        record = _make_record(overflow={})
        db = AsyncMock()

        result = FlierMatchResult(
            matched=False,
            line_number=0,
            row_data=None,
            error=None,
            confidence=0.0,
        )

        with patch(_PATCH_RS_GET, new=AsyncMock(return_value=record)):
            await extraction_service._apply_flier_match(db, 1, result)

        db.commit.assert_awaited_once()
