# Design Document: Rapidfuzz Flier Matching

## Overview

Replace the LLM-based flier name matching in `FlierMatchService` with local fuzzy string comparison using the `rapidfuzz` library. The new implementation scores every roster row against extracted flier data using a composite of name similarity and member number confirmation, then returns the single best match if it exceeds the applicable threshold.

The ExtractionService applies a tiered auto-accept policy: high-confidence matches are automatically accepted and applied to the record, while lower-confidence matches are flagged for manual review without overwriting existing data.

## Architecture

The redesign affects four layers:

1. **FlierMatchService** — rewritten core: loads TSV, scores candidates locally via `rapidfuzz.fuzz.WRatio`
2. **AppConfig / config.py** — relaxed validation: `flier_match_model` no longer required; adds `auto_accept_threshold`
3. **ExtractionService** — updated caller: removes `client` argument, stores confidence in overflow, implements tiered auto-accept logic
4. **main.py** — updated construction: removes `flier_match_model` from constructor call

The data flow becomes fully synchronous at the matching layer (though `match_flier()` remains async for API compatibility). No network calls are made during matching.

## Components

### FlierMatchService (Rewrite)

```python
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
    line_number: int          # 0 if no match
    row_data: dict[str, str] | None
    error: str | None
    confidence: float         # 0.0–1.0


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
        return self._enabled

    @property
    def row_count(self) -> int:
        return len(self._rows)

    def load(self) -> None:
        """Read and parse the TSV file into memory."""
        ...

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

        This no-club path is a primary use case, not a fallback. Flight cards
        frequently contain a member number without indicating which organization
        issued it. The algorithm must handle this as efficiently as the
        club-specified path.

        Returns list of (row_index, row_dict) tuples.
        """
        ...

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
        ...

    async def match_flier(
        self,
        flier_name: str | None,
        club: str | None,
        member_number: str | None,
        cert_level: int | None,
    ) -> FlierMatchResult:
        """Execute the full match flow against the loaded roster."""
        ...
```

### Matching Algorithm Detail

```
match_flier(flier_name, club, member_number, cert_level):
    if not enabled or not flier_name:
        return FlierMatchResult(matched=False, ..., confidence=0.0)

    # Phase 1: Member number lookup (if provided)
    member_confirmed_rows = set()
    if member_number:
        rows_by_number = _find_rows_by_member_number(member_number, club)
        member_confirmed_rows = {idx for idx, _ in rows_by_number}

    # Phase 2: Score ALL roster rows
    best_score = -1
    best_row = None
    best_line = 0
    best_confidence = 0.0
    best_member_confirmed = False

    for idx, row in enumerate(self._rows):
        is_confirmed = idx in member_confirmed_rows
        name_sim = _compute_name_similarity(flier_name, row["Name"])

        # Apply applicable threshold
        threshold = (
            self._member_confirmed_threshold if is_confirmed
            else self._name_only_threshold
        )
        if name_sim < threshold:
            continue

        # Compute composite score for ranking
        composite, confidence = _score_candidate(flier_name, row, is_confirmed)

        if composite > best_score:
            best_score = composite
            best_row = row
            best_line = idx + 2  # 1-indexed, header is line 1
            best_confidence = confidence
            best_member_confirmed = is_confirmed

    if best_row is None:
        return FlierMatchResult(matched=False, line_number=0,
                                row_data=None, error=None, confidence=0.0)

    return FlierMatchResult(matched=True, line_number=best_line,
                            row_data=best_row, error=None,
                            confidence=best_confidence)
```

### Member Number Lookup: No-Club Scenario

The `_find_rows_by_member_number()` method handles three distinct cases:

1. **Club specified, found in indicated column**: Return rows from that column.
2. **Club specified, not found in indicated column**: Fall back to searching the other column.
3. **No club specified** (primary pattern): Search both NAR and TRA columns in a single pass.

Case 3 is the most common in practice because many flight cards contain only a bare member number (e.g., "12345") without indicating "NAR" or "TRA". The implementation must:

- Iterate all rows checking both `row["NAR"]` and `row["TRA"]` against the provided number
- Return all matching rows regardless of which column matched
- Let the scoring phase (Phase 2) disambiguate if multiple rows match across columns

```python
def _find_rows_by_member_number(
    self,
    member_number: str,
    club: str | None,
) -> list[tuple[int, dict[str, str]]]:
    results = []
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
            if (row.get("NAR", "").strip() == normalized_number or
                    row.get("TRA", "").strip() == normalized_number):
                results.append((idx, row))

    return results
```

### Composite Scoring

The `_score_candidate` method computes:

- `name_similarity` = `WRatio(normalized_extracted, normalized_roster)` → 0–100 scale
- `member_bonus` = 20.0 if `member_confirmed` else 0.0
- `composite_score` = `name_similarity + member_bonus` (used for ranking only)
- `confidence` = `min(name_similarity / 100.0, 1.0)` boosted when member confirmed:
  - If member confirmed: `confidence = min((name_similarity + 20) / 100.0, 1.0)`
  - If name only: `confidence = name_similarity / 100.0`

The `confidence` value is clamped to [0.0, 1.0].

### Name Normalization

Before comparison, both the extracted name and each roster name are:
1. Stripped of leading/trailing whitespace
2. Converted to lowercase

`rapidfuzz.fuzz.WRatio` already handles token reordering (e.g., "Smith John" vs "John Smith") and partial matches internally.

### Config Changes

```python
# In AppConfig dataclass — add auto_accept_threshold field:
auto_accept_threshold: float = 0.95

# In load_config(): Remove the requirement that flier_match_model must be set
# when known_fliers_path is provided.

if known_fliers_path is not None and not known_fliers_path.exists():
    raise ConfigError(f"Known fliers file not found: {known_fliers_path}")
# The flier_match_model check is REMOVED
```

The `AppConfig.flier_match_model` field remains as `str | None` for backward compatibility but is no longer validated or used.

The `auto_accept_threshold` field is loaded from the config JSON (key `"auto_accept_threshold"`) with a default of `0.95` if not specified.

### ExtractionService Changes

#### Auto-Accept Threshold Constant

```python
# Module-level default, overridable via AppConfig
AUTO_ACCEPT_THRESHOLD: float = 0.95
```

In the `ExtractionService.__init__`, the threshold is read from config:

```python
self._auto_accept_threshold = config.auto_accept_threshold
```

#### Updated match_flier() Call Site

```python
# In _process(), the flier match call changes from:
result = await self._flier_match_service.match_flier(
    client,
    flier_name=record.flier_name,
    club=membership.get("club"),
    member_number=membership.get("member_number"),
    cert_level=membership.get("cert_level"),
)

# To (no client argument):
result = await self._flier_match_service.match_flier(
    flier_name=record.flier_name,
    club=membership.get("club"),
    member_number=membership.get("member_number"),
    cert_level=membership.get("cert_level"),
)
```

#### Tiered `_apply_flier_match()` Logic

The `_apply_flier_match()` method implements a three-way branch based on match result:

```python
async def _apply_flier_match(
    self,
    db,
    record_id: int,
    result,
) -> None:
    """Apply flier match result to the database record.

    Three cases:
    1. Error or no match → store status only
    2. High confidence (> auto_accept_threshold) → auto-accept,
       set flier_verified=True, apply row data to record
    3. Lower confidence (matched but <= auto_accept_threshold) → flag for
       review, set flier_verified=False, store candidate in overflow
       WITHOUT overwriting existing fields
    """
    from flight_card_scanner.services import record_service
    from flight_card_scanner.services.flier_match_service import FlierMatchResult

    record = await record_service.get(db, record_id)
    if record is None:
        return

    overflow = dict(record.overflow or {})

    if result.error:
        overflow["flier_match_status"] = "error"
        overflow["flier_match_error"] = str(result.error)
        record.overflow = overflow
        await db.commit()
        return

    if not result.matched:
        overflow["flier_match_status"] = "not_found"
        record.overflow = overflow
        await db.commit()
        return

    # Store confidence regardless of tier
    overflow["flier_match_confidence"] = result.confidence

    if result.confidence > self._auto_accept_threshold:
        # HIGH CONFIDENCE — auto-accept
        overflow["flier_match_status"] = "verified"
        record.flier_verified = True

        # Apply matched row data to the record
        row = result.row_data
        record.flier_name = row.get("Name") or record.flier_name

        # Update membership in overflow from authoritative roster data
        membership = overflow.get("membership", {})
        if row.get("NAR"):
            membership["club"] = "NAR"
            membership["member_number"] = row["NAR"]
        elif row.get("TRA"):
            membership["club"] = "TRA"
            membership["member_number"] = row["TRA"]
        if row.get("Level"):
            try:
                membership["cert_level"] = int(row["Level"])
            except (ValueError, TypeError):
                pass
        overflow["membership"] = membership

    else:
        # LOWER CONFIDENCE — flag for review, do NOT overwrite existing fields
        overflow["flier_match_status"] = "review"
        record.flier_verified = False

        # Store candidate data separately so reviewers can see it
        row = result.row_data
        candidate = {
            "name": row.get("Name"),
            "line_number": result.line_number,
        }
        if row.get("NAR"):
            candidate["club"] = "NAR"
            candidate["member_number"] = row["NAR"]
        elif row.get("TRA"):
            candidate["club"] = "TRA"
            candidate["member_number"] = row["TRA"]
        if row.get("Level"):
            candidate["cert_level"] = row["Level"]
        candidate["confidence"] = result.confidence
        overflow["flier_match_candidate"] = candidate

    record.overflow = overflow
    await db.commit()
```

### main.py Construction Changes

```python
# From:
flier_match_service = FlierMatchService(
    known_fliers_path=config.known_fliers_path,
    flier_match_model=config.flier_match_model,
)

# To:
flier_match_service = FlierMatchService(
    known_fliers_path=config.known_fliers_path,
)
```

## Data Models

### FlierMatchResult (Updated)

| Field | Type | Description |
|-------|------|-------------|
| `matched` | `bool` | Whether a match was found |
| `line_number` | `int` | 1-indexed line number in TSV (0 if no match) |
| `row_data` | `dict[str, str] \| None` | The matched row's column data |
| `error` | `str \| None` | Error message (always None in new impl — no network errors) |
| `confidence` | `float` | Match confidence 0.0–1.0 |

### Roster Row (TSV Columns)

| Column | Description |
|--------|-------------|
| `Name` | Full name of the flier |
| `NAR` | NAR member number (may be empty) |
| `TRA` | TRA member number (may be empty) |
| `Level` | Certification level (informational only) |

### Overflow Fields (ExtractionService)

| Field | Condition | Description |
|-------|-----------|-------------|
| `flier_match_status` | Always set | One of: `"verified"`, `"review"`, `"not_found"`, `"error"` |
| `flier_match_confidence` | When matched | Float 0.0–1.0 |
| `flier_match_candidate` | When status="review" | Dict with candidate name, club, member_number, cert_level, confidence |
| `flier_match_error` | When status="error" | Error message string |

## Error Handling

- **Empty/missing flier_name**: Returns `FlierMatchResult(matched=False, confidence=0.0)` immediately.
- **Empty roster (not enabled)**: Returns `FlierMatchResult(matched=False, confidence=0.0)`.
- **All candidates below threshold**: Returns `FlierMatchResult(matched=False, confidence=0.0)`.
- **TSV file read errors**: Raised during `load()` (before any matching occurs). The service sets `_enabled = False` and logs a warning for empty files.
- **No `error` field usage**: Since there are no network calls, the `error` field will always be `None`. It's retained for interface compatibility.

## Dependencies

- **Add**: `rapidfuzz` (PyPI package)
- **Remove from FlierMatchService**: `httpx` import (remains in ExtractionService for Ollama extraction calls)

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Best Match Optimality

*For any* roster and any extracted flier data, if `match_flier()` returns `matched=True` with a given row, then no other row in the roster has a higher composite score that also passes the applicable threshold.

**Validates: Requirements 1.2, 1.4**

### Property 2: Threshold Gating

*For any* roster and any extracted flier data, if the best candidate's name similarity is below `Member_Confirmed_Threshold` (when member-number-confirmed) or below `Name_Only_Threshold` (when not confirmed), then `match_flier()` returns `matched=False`.

**Validates: Requirements 1.3, 3.1, 3.2**

### Property 3: Member Number Lookup Completeness

*For any* roster containing a member number in either the NAR or TRA column, if the extracted data provides that member number (with or without a club hint), the service SHALL find the row and apply the member-confirmed threshold to it.

**Validates: Requirements 2.1, 2.2, 2.3**

### Property 4: Member-Confirmed Name Verification

*For any* roster row found by member number, if the name similarity between the extracted name and the row's name is below `Member_Confirmed_Threshold`, then that row SHALL NOT be returned as a match.

**Validates: Requirements 2.4**

### Property 5: Duplicate Member Number Disambiguation

*For any* roster with multiple rows sharing the same member number, `match_flier()` SHALL select the row with the highest name similarity score among those confirmed rows.

**Validates: Requirements 2.5**

### Property 6: Cert Level Independence

*For any* two calls to `match_flier()` with identical `flier_name`, `club`, and `member_number` but differing `cert_level` values, the returned `matched` status and `row_data` SHALL be identical.

**Validates: Requirements 3.3**

### Property 7: Confidence Score Invariant

*For any* input to `match_flier()`, the returned `confidence` value SHALL be a float in the range [0.0, 1.0] inclusive, AND `confidence == 0.0` if and only if `matched == False`.

**Validates: Requirements 4.3, 4.4**

### Property 8: Load Reflects TSV Content

*For any* valid TSV file with a header row and N data rows (N >= 0), after calling `load()`, the `enabled` property SHALL be `True` if and only if N > 0, and `row_count` SHALL equal N.

**Validates: Requirements 5.2, 5.3**

### Property 9: Config Accepts Known Fliers Without Model

*For any* valid configuration JSON that includes `known_fliers_path` pointing to an existing file but omits `flier_match_model`, `load_config()` SHALL return a valid `AppConfig` without raising `ConfigError`.

**Validates: Requirements 6.1, 6.3**

### Property 10: Auto-Accept Tiered Behavior

*For any* `FlierMatchResult` with `matched=True` and `confidence > Auto_Accept_Threshold`, `_apply_flier_match()` SHALL set `flier_verified=True`, set `flier_match_status` to `"verified"`, and apply the matched row's name and membership data to the record. Conversely, *for any* result with `matched=True` and `confidence <= Auto_Accept_Threshold`, `_apply_flier_match()` SHALL set `flier_verified=False`, set `flier_match_status` to `"review"`, store candidate data under `flier_match_candidate` in overflow, and SHALL NOT overwrite the record's existing `flier_name` or membership fields.

**Validates: Requirements 9.2, 9.3, 9.4, 9.5**
