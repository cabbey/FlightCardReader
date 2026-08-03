"""Tests for email column detection in FlierMatchService."""

import tempfile
from pathlib import Path

import pytest

from flight_card_scanner.services.flier_match_service import FlierMatchService


class TestEmailColumnDetection:
    """Test that _detect_columns detects email columns from various headers."""

    def test_email_header_standard(self, tmp_path) -> None:
        """Standard 'Email' header is detected."""
        tsv_content = (
            "Name\tEmail\tNAR Number\n"
            "John Smith\tjohn@example.com\t12345\n"
        )
        path = tmp_path / "fliers.tsv"
        path.write_text(tsv_content, encoding="utf-8")

        svc = FlierMatchService(known_fliers_path=path)
        svc.load()

        assert svc._col_email == "Email"

    def test_email_header_lowercase(self, tmp_path) -> None:
        """Lowercase 'email' header is detected."""
        tsv_content = (
            "Name\temail\tNAR Number\n"
            "John Smith\tjohn@example.com\t12345\n"
        )
        path = tmp_path / "fliers.tsv"
        path.write_text(tsv_content, encoding="utf-8")

        svc = FlierMatchService(known_fliers_path=path)
        svc.load()

        assert svc._col_email == "email"

    def test_email_header_hyphenated(self, tmp_path) -> None:
        """Hyphenated 'E-Mail' header is detected."""
        tsv_content = (
            "Name\tE-Mail\tNAR Number\n"
            "John Smith\tjohn@example.com\t12345\n"
        )
        path = tmp_path / "fliers.tsv"
        path.write_text(tsv_content, encoding="utf-8")

        svc = FlierMatchService(known_fliers_path=path)
        svc.load()

        assert svc._col_email == "E-Mail"

    def test_email_header_with_address(self, tmp_path) -> None:
        """'Email Address' header is detected (contains 'email')."""
        tsv_content = (
            "Name\tEmail Address\tNAR Number\n"
            "John Smith\tjohn@example.com\t12345\n"
        )
        path = tmp_path / "fliers.tsv"
        path.write_text(tsv_content, encoding="utf-8")

        svc = FlierMatchService(known_fliers_path=path)
        svc.load()

        assert svc._col_email == "Email Address"

    def test_email_header_uppercase(self, tmp_path) -> None:
        """'EMAIL' header is detected."""
        tsv_content = (
            "Name\tEMAIL\tNAR Number\n"
            "John Smith\tjohn@example.com\t12345\n"
        )
        path = tmp_path / "fliers.tsv"
        path.write_text(tsv_content, encoding="utf-8")

        svc = FlierMatchService(known_fliers_path=path)
        svc.load()

        assert svc._col_email == "EMAIL"

    def test_no_email_column_uses_default(self, tmp_path) -> None:
        """If no email column header is found, default 'Email' is kept."""
        tsv_content = (
            "Name\tNAR\tTRA\tLevel\n"
            "John Smith\t12345\t\t3\n"
        )
        path = tmp_path / "fliers.tsv"
        path.write_text(tsv_content, encoding="utf-8")

        svc = FlierMatchService(known_fliers_path=path)
        svc.load()

        # Default value is preserved
        assert svc._col_email == "Email"

    def test_email_column_with_all_other_columns(self, tmp_path) -> None:
        """Email column coexists properly with all other detected columns."""
        tsv_content = (
            "Name\tCertification Level\tTRA Number\tNAR Number\tEmail\n"
            "John Smith\tL2\t54321\t12345\tjohn@example.com\n"
        )
        path = tmp_path / "fliers.tsv"
        path.write_text(tsv_content, encoding="utf-8")

        svc = FlierMatchService(known_fliers_path=path)
        svc.load()

        assert svc._col_name == "Name"
        assert svc._col_nar == "NAR Number"
        assert svc._col_tra == "TRA Number"
        assert svc._col_level == "Certification Level"
        assert svc._col_email == "Email"

    def test_e_mail_lowercase_hyphenated(self, tmp_path) -> None:
        """'e-mail' lowercase hyphenated header is detected."""
        tsv_content = (
            "Name\te-mail\tNAR Number\n"
            "John Smith\tjohn@example.com\t12345\n"
        )
        path = tmp_path / "fliers.tsv"
        path.write_text(tsv_content, encoding="utf-8")

        svc = FlierMatchService(known_fliers_path=path)
        svc.load()

        assert svc._col_email == "e-mail"
