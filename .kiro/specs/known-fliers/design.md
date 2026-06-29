# Design Document: Known Fliers

## Architecture Overview

The Known Fliers feature adds a post-extraction verification step that matches extracted flier information against a pre-loaded TSV roster using an LLM for fuzzy matching. It integrates into the existing extraction pipeline as a new service (`FlierMatchService`) invoked after the ThrustCurve motor lookup.

```
Scan → ExtractionService._process()
         ├── _call_ollama() (vision extraction)
         ├── resolve_flight_date()
         ├── apply_extraction()
         ├── ThrustCurve motor lookup
         └── FlierMatchService.match_flier()  ← NEW
```

The feature is opt-in: when `known_fliers_path` is absent from config, the entire matching step is skipped.

## Components

### 1. Configuration Extension (`config.py`)

Two new optional fields on `AppConfig`:

```python
@dataclass
class AppConfig:
    # ... existing fields ...
    known_fliers_path: Path | None = None
    flier_match_model: str | None = None
```

**Validation rules:**
- If `known_fliers_path` is present but `flier_match_model` is absent → `ConfigError`
- If `known_fliers_path` is present but the file doesn't exist on disk → `ConfigError`
- If both are absent → feature disabled (INFO log)

### 2. FlierMatchService (`flight_card_scanner/services/flier_match_service.py`)

New service responsible for:
- Loading and parsing the TSV file at startup
- Building the LLM prompt
- Calling Ollama with a text-only chat request
- Parsing the response and applying match results

```python
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
        ...

    def build_prompt(
        self,
        flier_name: str | None,
        club: str | None,
        member_number: str | None,
        cert_level: int | None,
    ) -> str:
        """Build the matching prompt with numbered flier lines and extracted data."""
        ...

    def build_payload(
        self,
        prompt: str,
    ) -> dict:
        """Build the Ollama /api/chat payload (text-only, no images)."""
        ...

    def parse_response(self, raw_content: str) -> int:
        """Parse LLM response as an integer line number.

        Returns:
            The parsed line number (0 means no match).

        Raises:
            ValueError: If the response cannot be parsed as an integer.
        """
        ...

    def get_row_by_line_number(self, line_number: int) -> dict[str, str] | None:
        """Get a data row by its 1-indexed line number (line 1 = header).

        Returns None if line_number is out of bounds.
        Data row 1 is at line_number 2 (since line 1 is the header).
        """
        ...

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
        ...
```

### 3. FlierMatchResult (data class)

```python
from dataclasses import dataclass
from typing import Any

@dataclass
class FlierMatchResult:
    """Result of a flier match attempt."""
    matched: bool
    line_number: int  # 0 if no match
    row_data: dict[str, str] | None  # The matched row, or None
    error: str | None  # Error message if match failed
```

### 4. Database Schema Change (`models.py`)

New column on `FlightRecord`:

```python
class FlightRecord(Base):
    # ... existing columns ...
    flier_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
```

Migration: Add column with `ALTER TABLE flight_records ADD COLUMN flier_verified BOOLEAN NOT NULL DEFAULT 0`.

### 5. Integration Point (`extraction_service.py`)

In `ExtractionService._process()`, after the ThrustCurve lookup block:

```python
# Post-extraction: flier verification via known fliers list
if self._flier_match_service and self._flier_match_service.enabled:
    try:
        async with self._session_factory() as db:
            record = await record_service.get(db, record_id)
            if record:
                # Extract membership info from overflow
                membership = (record.overflow or {}).get("membership", {})
                result = await self._flier_match_service.match_flier(
                    client,
                    flier_name=record.flier_name,
                    club=membership.get("club"),
                    member_number=membership.get("member_number"),
                    cert_level=membership.get("cert_level"),
                )
                await self._apply_flier_match(db, record_id, result)
    except Exception as exc:
        logger.warning(
            "Flier verification failed for record %d: %s",
            record_id,
            exc,
        )
```

## Data Flow

```
1. Startup:
   config.json → load_config() → AppConfig (with known_fliers_path, flier_match_model)
                                       ↓
   FlierMatchService.load() → TSV parsed into list[dict[str, str]]

2. Per-record (in _process):
   FlightRecord (extracted) 
     → build_prompt(flier_name, club, member_number, cert_level)
     → POST /api/chat to Ollama (text-only, flier_match_model)
     → parse_response() → integer line number
     → line > 0 and in range?
         YES → update flier_name, overflow.membership, flier_verified=True
         NO (0) → set extraction_status="flier_not_found", preserve original data
         ERROR → log warning, leave status as "extracted"
```

## Prompt Design

The prompt follows a structured three-section layout:

```
Section 1: Instructions
- Explain the task: match a flier against a numbered list
- Instruct to return the line number (1-indexed) or 0 for no match
- Emphasize fuzzy matching (name variations, OCR errors)

Section 2: Known Fliers List (numbered lines)
- Line 1: [header row as-is]
- Line 2: [first data row]
- Line 3: [second data row]
- ...
- Line N: [last data row]

Section 3: Extracted Data
- Flier name: {flier_name}
- Club: {club}
- Member number: {member_number}
- Certification level: {cert_level}
```

Example prompt:

```
You are matching a flier extracted from a handwritten flight card against a list of known club members.

Instructions:
- Compare the extracted flier information below against the numbered list of known fliers.
- Return ONLY the line number of the best match. Line 1 is the header row; data starts at line 2.
- If no sufficiently close match exists, return 0.
- Account for common OCR errors: O/0, I/1, similar-sounding names, abbreviations.

Known Fliers List:
1	Name	NAR	TRA	Level
2	John Smith	12345		3
3	Jane Doe		54321	2
4	Bob Johnson	67890	11111	1
...

Extracted flier information:
- Name: Jon Smith
- Club: NAR
- Member number: 12345
- Certification level: 3

Respond with ONLY the line number (an integer), nothing else.
```

## LLM Request Payload

```python
{
    "model": self._model,  # e.g., "qwen2.5:7b"
    "messages": [
        {
            "role": "user",
            "content": prompt_text,
            # NO "images" key
        }
    ],
    "stream": False,
    "options": {
        "temperature": 0,
        "num_ctx": 32768,  # accommodate 500+ row prompts
        "num_predict": 16,  # only need a short integer response
    },
}
```

Key differences from the vision extraction request:
- No `"images"` field in messages
- Uses `flier_match_model` instead of `"qwen3-vl"`
- `num_predict` is very small (only need a number)
- No `"format"` schema (plain text response)

## Match Result Application

```python
async def _apply_flier_match(
    self,
    db: AsyncSession,
    record_id: int,
    result: FlierMatchResult,
) -> None:
    """Apply flier match result to the database record."""
    record = await record_service.get(db, record_id)
    if record is None:
        return

    if result.error:
        # Parse failure — set status to flier_match_failed
        record.extraction_status = "flier_match_failed"
        await db.commit()
        return

    if not result.matched:
        # No match found — set status to flier_not_found, preserve original data
        record.extraction_status = "flier_not_found"
        await db.commit()
        return

    # Successful match — update record with authoritative data
    row = result.row_data
    record.flier_name = row.get("Name") or record.flier_name
    record.flier_verified = True

    # Update membership in overflow
    overflow = dict(record.overflow or {})
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
    record.overflow = overflow

    await db.commit()
```

## Error Handling

| Scenario | Behavior |
|----------|----------|
| `known_fliers_path` not in config | Feature disabled, INFO log, no error |
| TSV file not found at configured path | `ConfigError` at startup |
| TSV empty or header-only | WARNING log, feature disabled |
| `flier_match_model` missing when path present | `ConfigError` at startup |
| Ollama endpoint error during match | WARNING log, record stays `extracted` |
| LLM returns non-integer | WARNING log, status → `flier_match_failed` |
| LLM returns line number > row count | WARNING log, treat as no match |
| LLM returns 0 | Status → `flier_not_found`, original data preserved |

## Lifecycle and Startup

1. `load_config()` validates new fields, raises `ConfigError` on violations
2. In `lifespan()`, after `ThrustCurveService.startup()`:
   ```python
   flier_match_service = None
   if config.known_fliers_path:
       flier_match_service = FlierMatchService(
           known_fliers_path=config.known_fliers_path,
           flier_match_model=config.flier_match_model,
       )
       flier_match_service.load()
   ```
3. Pass `flier_match_service` to `ExtractionService.__init__()`
4. `ExtractionService` stores it and calls it in `_process()` after ThrustCurve

## Model Recommendation

For the `flier_match_model`, the recommended model is **qwen2.5:7b**.

Rationale:
- Strong text reasoning and instruction following at 7B parameters
- Handles structured prompts with 500+ lines well within 32K context window
- Good at fuzzy string matching tasks (name variations, number transpositions)
- Runs comfortably on consumer GPUs (8GB VRAM) and Apple Silicon
- Available in Ollama library with quantized variants for resource-constrained setups
- Fast inference for the simple integer-output task (< 1 second typical)

Alternative options: `llama3.1:8b`, `gemma2:9b`, `phi3:medium` — all viable but qwen2.5:7b shows stronger performance on structured text matching benchmarks.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Config fields round-trip

*For any* valid filesystem path string and any non-empty model name string, a config JSON containing `known_fliers_path` and `flier_match_model` set to those values should parse successfully via `load_config()` and produce an `AppConfig` whose fields reflect the original values.

**Validates: Requirements 1.1, 1.4**

### Property 2: TSV parsing preserves data

*For any* valid TSV file with a header row and N data rows (N ≥ 1), calling `FlierMatchService.load()` should produce exactly N structured records, and each record's fields should correspond to the values in the original TSV row (keyed by header column names).

**Validates: Requirements 2.1, 2.2, 2.3**

### Property 3: Prompt contains all required data

*For any* known fliers list of N rows and any extracted flier information (name, club, member_number, cert_level), the prompt generated by `build_prompt()` should contain every row from the flier list prefixed with its 1-indexed line number, and should contain the extracted flier name, club, member number, and certification level.

**Validates: Requirements 4.2, 4.3, 8.1**

### Property 4: LLM response integer parsing

*For any* string that contains a valid non-negative integer (possibly surrounded by whitespace or newlines), `parse_response()` should extract and return that integer. *For any* string that does not contain a valid non-negative integer, `parse_response()` should raise `ValueError`.

**Validates: Requirements 4.6, 4.7**

### Property 5: Successful match updates record correctly

*For any* line number L where 1 < L ≤ (row_count + 1), applying a match result with that line number should: (a) set `flier_verified` to True, (b) update `flier_name` to the matched row's name value, and (c) update the overflow membership data with values from the matched row.

**Validates: Requirements 3.2, 5.1, 5.2, 5.3, 5.4**

### Property 6: No match preserves original data

*For any* FlightRecord with existing flier_name and membership data, when the match result indicates no match (line number = 0), the record should have: (a) `flier_verified` remain False, (b) `extraction_status` set to `flier_not_found`, and (c) the original `flier_name` and overflow membership data unchanged.

**Validates: Requirements 3.3, 6.1, 6.2, 6.3**

### Property 7: Flier match payload is text-only

*For any* call to `build_payload()`, the resulting request dict should contain no `"images"` key in any message, and should use the configured `flier_match_model` as the `"model"` value.

**Validates: Requirements 7.3**
