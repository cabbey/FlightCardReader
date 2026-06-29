"""Known flier matching service.

Provides:
- TSV file loading and parsing of known flier data
- LLM-based fuzzy matching of extracted flier information against the roster
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


@dataclass
class FlierMatchResult:
    """Result of a flier match attempt."""

    matched: bool
    line_number: int  # 0 if no match
    row_data: dict[str, str] | None  # The matched row, or None
    error: str | None  # Error message if match failed


class FlierMatchService:
    """Manages known flier list loading and LLM-based matching."""

    def __init__(
        self,
        known_fliers_path: Path,
        flier_match_model: str,
    ) -> None:
        self._path = known_fliers_path
        self._model = flier_match_model
        self._headers: list[str] = []
        self._rows: list[dict[str, str]] = []
        self._raw_lines: list[str] = []
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

        self._rows = [
            dict(zip(self._headers, row)) for row in data_rows
        ]
        self._raw_lines = ["\t".join(row) for row in rows]
        self._enabled = True
        logger.info(
            "Loaded %d known fliers from %s",
            len(self._rows),
            self._path,
        )

    def build_prompt(
        self,
        flier_name: str | None,
        club: str | None,
        member_number: str | None,
        cert_level: int | None,
    ) -> str:
        """Build the matching prompt with numbered flier lines and extracted data.

        The prompt has three sections:
        1. Instructions explaining the matching task
        2. Numbered known fliers list (1-indexed, header at line 1)
        3. Extracted data from the flight card
        """
        # Section 1: Instructions
        instructions = (
            "You are matching a flier extracted from a handwritten flight card "
            "against a list of known club members.\n"
            "\n"
            "Instructions:\n"
            "- Compare the extracted flier information below against the numbered "
            "list of known fliers.\n"
            "- Return ONLY the line number of the best match. Line 1 is the header "
            "row; data starts at line 2.\n"
            "- If no sufficiently close match exists, return 0.\n"
            "- Account for common OCR errors: O/0, I/1, similar-sounding names, "
            "abbreviations."
        )

        # Section 2: Numbered known fliers list
        numbered_lines = "\n".join(
            f"{i + 1}\t{line}" for i, line in enumerate(self._raw_lines)
        )
        flier_list_section = f"Known Fliers List:\n{numbered_lines}"

        # Section 3: Extracted data
        extracted_section = (
            "Extracted flier information:\n"
            f"- Name: {flier_name or ''}\n"
            f"- Club: {club or ''}\n"
            f"- Member number: {member_number or ''}\n"
            f"- Certification level: {cert_level if cert_level is not None else ''}"
        )

        # Final instruction
        closing = "Respond with ONLY the line number (an integer), nothing else."

        return f"{instructions}\n\n{flier_list_section}\n\n{extracted_section}\n\n{closing}"

    def build_payload(self, prompt: str) -> dict:
        """Build the Ollama /api/chat payload (text-only, no images).

        Returns a dict suitable for POSTing to the Ollama /api/chat endpoint.
        Uses the configured flier_match_model, temperature 0 for deterministic
        output, a large context window for big rosters, and minimal prediction
        length since only an integer is expected.
        """
        return {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            "stream": False,
            "options": {
                "temperature": 0,
                "num_ctx": 32768,
                "num_predict": 16,
            },
        }

    def parse_response(self, raw_content: str) -> int:
        """Parse LLM response as an integer line number.

        Returns:
            The parsed line number (0 means no match).

        Raises:
            ValueError: If the response cannot be parsed as an integer.
        """
        stripped = raw_content.strip()
        return int(stripped)

    def get_row_by_line_number(self, line_number: int) -> dict[str, str] | None:
        """Get a data row by its 1-indexed line number (line 1 = header).

        Returns None if line_number is out of bounds.
        Data row 1 is at line_number 2 (since line 1 is the header).
        """
        # Line 1 is the header, so data rows start at line 2.
        # line_number 2 → index 0, line_number 3 → index 1, etc.
        row_index = line_number - 2
        if row_index < 0 or row_index >= len(self._rows):
            return None
        return self._rows[row_index]

    async def match_flier(
        self,
        client: httpx.AsyncClient,
        flier_name: str | None,
        club: str | None,
        member_number: str | None,
        cert_level: int | None,
    ) -> FlierMatchResult:
        """Execute the full match flow: build prompt, call LLM, parse result.

        Returns a FlierMatchResult with status and optional matched data.
        """
        prompt = self.build_prompt(flier_name, club, member_number, cert_level)
        payload = self.build_payload(prompt)

        # POST to Ollama /api/chat
        try:
            response = await client.post("/api/chat", json=payload)
            response.raise_for_status()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            return FlierMatchResult(
                matched=False,
                line_number=0,
                row_data=None,
                error=str(exc),
            )

        # Extract content from response
        data = response.json()
        raw_content = data["message"]["content"]

        # Parse the integer line number
        try:
            line_number = self.parse_response(raw_content)
        except ValueError:
            return FlierMatchResult(
                matched=False,
                line_number=0,
                row_data=None,
                error=f"Failed to parse LLM response: {raw_content}",
            )

        # Line number 0 means no match
        if line_number == 0:
            return FlierMatchResult(
                matched=False,
                line_number=0,
                row_data=None,
                error=None,
            )

        # Validate line number is in bounds
        row = self.get_row_by_line_number(line_number)
        if row is None:
            logger.warning(
                "LLM returned line number %d which is out of bounds (row_count=%d)",
                line_number,
                self.row_count,
            )
            return FlierMatchResult(
                matched=False,
                line_number=line_number,
                row_data=None,
                error=None,
            )

        # Successful match
        return FlierMatchResult(
            matched=True,
            line_number=line_number,
            row_data=row,
            error=None,
        )
