"""Quick test to validate column detection with real-world headers."""

import tempfile
from pathlib import Path

import pytest

from flight_card_scanner.services.flier_match_service import FlierMatchService


class TestColumnDetection:
    """Test that _detect_columns properly handles varying header names."""

    def test_real_world_headers(self) -> None:
        """Headers like 'NAR Number', 'TRA Number', 'Certification Level' are detected."""
        tsv_content = (
            "Name\tCertification Level\tTRA Number\tNAR Number\n"
            "John Smith\tL2\t54321\t12345\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".tsv", delete=False, encoding="utf-8"
        ) as f:
            f.write(tsv_content)
            path = Path(f.name)

        svc = FlierMatchService(known_fliers_path=path)
        svc.load()

        assert svc._col_name == "Name"
        assert svc._col_nar == "NAR Number"
        assert svc._col_tra == "TRA Number"
        assert svc._col_level == "Certification Level"

    def test_extract_roster_data_with_real_headers(self) -> None:
        """extract_roster_data correctly parses L2-style cert levels."""
        tsv_content = (
            "Name\tCertification Level\tTRA Number\tNAR Number\n"
            "John Smith\tL2\t54321\t12345\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".tsv", delete=False, encoding="utf-8"
        ) as f:
            f.write(tsv_content)
            path = Path(f.name)

        svc = FlierMatchService(known_fliers_path=path)
        svc.load()

        row = svc._rows[0]
        data = svc.extract_roster_data(row)

        assert data["name"] == "John Smith"
        assert data["nar_number"] == "12345"
        assert data["tra_number"] == "54321"
        assert data["cert_level"] == 2

    def test_member_number_lookup_with_real_headers(self) -> None:
        """Member number lookup works with detected column names."""
        tsv_content = (
            "Name\tCertification Level\tTRA Number\tNAR Number\n"
            "John Smith\tL2\t54321\t12345\n"
            "Jane Doe\tL1\t\t67890\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".tsv", delete=False, encoding="utf-8"
        ) as f:
            f.write(tsv_content)
            path = Path(f.name)

        svc = FlierMatchService(known_fliers_path=path)
        svc.load()

        # Search NAR column
        results = svc._find_rows_by_member_number("12345", "NAR")
        assert len(results) == 1
        assert results[0][1]["Name"] == "John Smith"

        # Search TRA column
        results = svc._find_rows_by_member_number("54321", "TRA")
        assert len(results) == 1
        assert results[0][1]["Name"] == "John Smith"

        # Search without club
        results = svc._find_rows_by_member_number("67890", None)
        assert len(results) == 1
        assert results[0][1]["Name"] == "Jane Doe"

    @pytest.mark.anyio
    async def test_full_match_with_real_headers(self) -> None:
        """match_flier works end-to-end with real-world column names."""
        tsv_content = (
            "Name\tCertification Level\tTRA Number\tNAR Number\n"
            "John Smith\tL2\t54321\t12345\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".tsv", delete=False, encoding="utf-8"
        ) as f:
            f.write(tsv_content)
            path = Path(f.name)

        svc = FlierMatchService(known_fliers_path=path)
        svc.load()

        result = await svc.match_flier(
            flier_name="John Smith",
            club="NAR",
            member_number="12345",
            cert_level=2,
        )

        assert result.matched is True
        assert result.row_data is not None
        assert result.row_data["Name"] == "John Smith"
        assert result.confidence >= 0.95

    def test_default_headers_still_work(self) -> None:
        """Original short headers (NAR, TRA, Level) still detected correctly."""
        tsv_content = (
            "Name\tNAR\tTRA\tLevel\n"
            "John Smith\t12345\t\t3\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".tsv", delete=False, encoding="utf-8"
        ) as f:
            f.write(tsv_content)
            path = Path(f.name)

        svc = FlierMatchService(known_fliers_path=path)
        svc.load()

        assert svc._col_name == "Name"
        assert svc._col_nar == "NAR"
        assert svc._col_tra == "TRA"
        assert svc._col_level == "Level"

        data = svc.extract_roster_data(svc._rows[0])
        assert data["name"] == "John Smith"
        assert data["nar_number"] == "12345"
        assert data["tra_number"] is None
        assert data["cert_level"] == 3

    def test_cert_level_l1_l2_l3_parsing(self) -> None:
        """extract_roster_data handles L1, L2, L3 format."""
        tsv_content = (
            "Name\tCertification Level\tTRA Number\tNAR Number\n"
            "Alice\tL1\t\t11111\n"
            "Bob\tL3\t22222\t\n"
            "Charlie\t2\t33333\t\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".tsv", delete=False, encoding="utf-8"
        ) as f:
            f.write(tsv_content)
            path = Path(f.name)

        svc = FlierMatchService(known_fliers_path=path)
        svc.load()

        assert svc.extract_roster_data(svc._rows[0])["cert_level"] == 1
        assert svc.extract_roster_data(svc._rows[1])["cert_level"] == 3
        assert svc.extract_roster_data(svc._rows[2])["cert_level"] == 2
