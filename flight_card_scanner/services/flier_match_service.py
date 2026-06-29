"""Known flier matching service.

Provides:
- TSV file loading and parsing of known flier data
- Rapidfuzz-based fuzzy matching of extracted flier information against the roster
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz

logger = logging.getLogger(__name__)


@dataclass
class FlierMatchResult:
    """Result of a flier match attempt."""

    matched: bool
    line_number: int  # 0 if no match
    row_data: dict[str, str] | None  # The matched row, or None
    error: str | None  # Error message if match failed
    confidence: float  # 0.0–1.0


class FlierMatchService:
    """Manages known flier list loading and rapidfuzz-based matching."""

    DEFAULT_NAME_ONLY_THRESHOLD: float = 80.0
    DEFAULT_MEMBER_CONFIRMED_THRESHOLD: float = 60.0

    def __init__(
        self,
        known_fliers_path: Path,
        *,
        name_only_threshold: float | None = None,
        member_confirmed_threshold: float | None = None,
    ) -> None:
        self._path = known_fliers_path
        self._name_only_threshold = (
            name_only_threshold
            if name_only_threshold is not None
            else self.DEFAULT_NAME_ONLY_THRESHOLD
        )
        self._member_confirmed_threshold = (
            member_confirmed_threshold
            if member_confirmed_threshold is not None
            else self.DEFAULT_MEMBER_CONFIRMED_THRESHOLD
        )
        self._headers: list[str] = []
        self._rows: list[dict[str, str]] = []
        self._enabled: bool = False

    @property
    def enabled(self) -> bool:
        """Whether flier matching is active (file loaded with data rows)."""
        return self._enabled

    @property
    def row_count(self) -> int:
        """Number of data rows (excluding header)."""
        return len(self._rows)

    def load(self) -> None:
        """Read and parse the TSV file into memory.

        Sets self._enabled = True if at least one data row exists.
        Logs WARNING and disables if file is empty or header-only.
        """
        with open(self._path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f, delimiter="\t")
            rows = list(reader)

        if not rows:
            logger.warning(
                "Known fliers file is empty: %s — flier verification disabled",
                self._path,
            )
            self._enabled = False
            return

        self._headers = rows[0]

        data_rows = rows[1:]
        if not data_rows:
            logger.warning(
                "Known fliers file contains only a header row: %s "
                "— flier verification disabled",
                self._path,
            )
            self._enabled = False
            return

        self._rows = [dict(zip(self._headers, row)) for row in data_rows]
        self._enabled = True
        logger.info(
            "Loaded %d known fliers from %s",
            len(self._rows),
            self._path,
        )

    @staticmethod
    def _normalize_name(name: str) -> str:
        """Lowercase and strip whitespace for comparison."""
        return name.strip().lower()

    def _compute_name_similarity(self, extracted_name: str, roster_name: str) -> float:
        """Compute similarity score (0–100) using rapidfuzz.fuzz.WRatio."""
        a = self._normalize_name(extracted_name)
        b = self._normalize_name(roster_name)
        return fuzz.WRatio(a, b)

    def _find_rows_by_member_number(
        self,
        member_number: str,
        club: str | None,
    ) -> list[tuple[int, dict[str, str]]]:
        """Find roster rows matching the given member number.

        Search order:
        1. If club is specified, search that column first, then the other.
        2. If no club specified (common pattern — many flight cards contain
           only a bare number), search BOTH NAR and TRA columns simultaneously.

        Returns list of (row_index, row_dict) tuples.
        """
        results: list[tuple[int, dict[str, str]]] = []
        normalized_number = member_number.strip()

        if club:
            # Search indicated column first
            primary_col = club.upper()  # "NAR" or "TRA"
            other_col = "TRA" if primary_col == "NAR" else "NAR"

            for idx, row in enumerate(self._rows):
                if row.get(primary_col, "").strip() == normalized_number:
                    results.append((idx, row))

            # Fall back to other column if nothing found
            if not results:
                for idx, row in enumerate(self._rows):
                    if row.get(other_col, "").strip() == normalized_number:
                        results.append((idx, row))
        else:
            # No club specified — search both columns (primary use case)
            for idx, row in enumerate(self._rows):
                if (
                    row.get("NAR", "").strip() == normalized_number
                    or row.get("TRA", "").strip() == normalized_number
                ):
                    results.append((idx, row))

        return results

    def _score_candidate(
        self,
        flier_name: str,
        row: dict[str, str],
        member_confirmed: bool,
    ) -> tuple[float, float]:
        """Score a single candidate row.

        Returns (composite_score, confidence) where:
        - composite_score is used for ranking (internal)
        - confidence is the 0.0–1.0 value stored in the result
        """
        name_similarity = self._compute_name_similarity(flier_name, row.get("Name", ""))
        member_bonus = 20.0 if member_confirmed else 0.0
        composite_score = name_similarity + member_bonus

        if member_confirmed:
            confidence = min((name_similarity + 20.0) / 100.0, 1.0)
        else:
            confidence = name_similarity / 100.0

        return composite_score, confidence

    async def match_flier(
        self,
        flier_name: str | None,
        club: str | None,
        member_number: str | None,
        cert_level: int | None,
    ) -> FlierMatchResult:
        """Execute the full match flow against the loaded roster.

        Scores all roster rows using rapidfuzz name similarity, applies
        thresholds, and returns the best match.
        """
        if not self._enabled or not flier_name:
            return FlierMatchResult(
                matched=False,
                line_number=0,
                row_data=None,
                error=None,
                confidence=0.0,
            )

        # Phase 1: Member number lookup (if provided)
        member_confirmed_rows: set[int] = set()
        if member_number:
            rows_by_number = self._find_rows_by_member_number(member_number, club)
            member_confirmed_rows = {idx for idx, _ in rows_by_number}

        # Phase 2: Score ALL roster rows
        best_score: float = -1.0
        best_row: dict[str, str] | None = None
        best_line: int = 0
        best_confidence: float = 0.0

        for idx, row in enumerate(self._rows):
            is_confirmed = idx in member_confirmed_rows
            name_sim = self._compute_name_similarity(flier_name, row.get("Name", ""))

            # Apply applicable threshold
            threshold = (
                self._member_confirmed_threshold
                if is_confirmed
                else self._name_only_threshold
            )
            if name_sim < threshold:
                continue

            # Compute composite score for ranking
            composite, confidence = self._score_candidate(flier_name, row, is_confirmed)

            if composite > best_score:
                best_score = composite
                best_row = row
                best_line = idx + 2  # 1-indexed, header is line 1
                best_confidence = confidence

        if best_row is None:
            return FlierMatchResult(
                matched=False,
                line_number=0,
                row_data=None,
                error=None,
                confidence=0.0,
            )

        return FlierMatchResult(
            matched=True,
            line_number=best_line,
            row_data=best_row,
            error=None,
            confidence=best_confidence,
        )
