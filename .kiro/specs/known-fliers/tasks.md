# Implementation Plan: Known Fliers

## Overview

Add a post-extraction flier verification step that matches extracted flier information against a pre-loaded TSV roster using an LLM for fuzzy matching. The feature integrates as a new `FlierMatchService` invoked after ThrustCurve motor lookups in the extraction pipeline. It is opt-in via `known_fliers_path` and `flier_match_model` config fields.

## Tasks

- [ ] 1. Configuration and database schema
  - [ ] 1.1 Add `known_fliers_path` and `flier_match_model` fields to `AppConfig` and `load_config()`
    - Add two new optional fields to `AppConfig` dataclass in `flight_card_scanner/config.py`: `known_fliers_path: Path | None = None` and `flier_match_model: str | None = None`
    - In `load_config()`, parse the new JSON keys with appropriate defaults (None when absent)
    - Validate: if `known_fliers_path` is set but `flier_match_model` is not, raise `ConfigError`
    - Validate: if `known_fliers_path` is set but the file doesn't exist, raise `ConfigError`
    - When both are absent, log INFO that flier verification is disabled
    - Wire the new fields into the `AppConfig` return value
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

  - [ ]* 1.2 Write property test for config field round-trip (Property 1)
    - **Property 1: Config fields round-trip**
    - **Validates: Requirements 1.1, 1.4**
    - For any valid path string and non-empty model name string, a config JSON containing both fields should parse via `load_config()` and produce an `AppConfig` with matching field values

  - [ ] 1.3 Add `flier_verified` column to `FlightRecord` model
    - In `flight_card_scanner/models.py`, add `flier_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")`
    - The column uses SQLAlchemy `server_default="0"` so existing rows get a default value when the schema is applied
    - _Requirements: 3.1, 3.2, 3.3_

- [ ] 2. Implement FlierMatchService core
  - [ ] 2.1 Create `flight_card_scanner/services/flier_match_service.py` with class skeleton and TSV loading
    - Create the new file with `FlierMatchService` class, `FlierMatchResult` dataclass, `__init__`, `load()`, `enabled` property, and `row_count` property
    - `load()` reads the TSV file, stores first row as headers and remaining rows as `list[dict[str, str]]`
    - Set `_enabled = True` if at least one data row exists; log WARNING and disable if empty or header-only
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

  - [ ]* 2.2 Write property test for TSV parsing (Property 2)
    - **Property 2: TSV parsing preserves data**
    - **Validates: Requirements 2.1, 2.2, 2.3**
    - For any valid TSV with header + N data rows (N ≥ 1), `load()` should produce exactly N records with fields matching the original row values keyed by header names

  - [ ] 2.3 Implement `build_prompt()` and `build_payload()` methods
    - `build_prompt()` builds the three-section prompt: instructions, numbered flier list (1-indexed with header at line 1), and extracted data (flier_name, club, member_number, cert_level)
    - `build_payload()` returns the Ollama `/api/chat` dict with text-only messages (no images), `flier_match_model`, `temperature: 0`, `num_ctx: 32768`, `num_predict: 16`, `stream: False`
    - _Requirements: 4.2, 4.3, 4.4, 4.5, 7.3, 8.1, 8.2, 8.3_

  - [ ]* 2.4 Write property test for prompt contents (Property 3)
    - **Property 3: Prompt contains all required data**
    - **Validates: Requirements 4.2, 4.3, 8.1**
    - For any flier list of N rows and any extracted flier info, the generated prompt must contain every row prefixed with its line number, and must contain the extracted name, club, member number, and cert level

  - [ ]* 2.5 Write property test for payload structure (Property 7)
    - **Property 7: Flier match payload is text-only**
    - **Validates: Requirements 7.3**
    - For any call to `build_payload()`, the result must contain no `"images"` key in any message, and must use the configured `flier_match_model` as the `"model"` value

  - [ ] 2.6 Implement `parse_response()` and `get_row_by_line_number()` methods
    - `parse_response()` strips whitespace/newlines from raw LLM content and parses as int; raises `ValueError` on non-integer input
    - `get_row_by_line_number()` returns the data row at the given 1-indexed line number (line 1 = header, line 2 = first data row), or None if out of bounds
    - _Requirements: 4.6, 4.7, 5.1, 5.5_

  - [ ]* 2.7 Write property test for response parsing (Property 4)
    - **Property 4: LLM response integer parsing**
    - **Validates: Requirements 4.6, 4.7**
    - For any string containing a valid non-negative integer (with optional surrounding whitespace), `parse_response()` returns that integer; for non-integer strings, it raises `ValueError`

  - [ ] 2.8 Implement `match_flier()` async method
    - Orchestrates the full match flow: `build_prompt()` → `build_payload()` → POST to Ollama → `parse_response()` → validate line number → return `FlierMatchResult`
    - Handle HTTP errors by returning a result with `error` set
    - Handle `ValueError` from parse by returning a result with `error` set
    - Handle line number out of bounds (> row_count + 1) by returning `matched=False`
    - _Requirements: 4.1, 4.6, 4.7, 5.1, 5.5, 7.2, 7.4_

- [ ] 3. Checkpoint
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 4. Integration into extraction pipeline
  - [ ] 4.1 Initialize `FlierMatchService` in application lifespan (`main.py`)
    - In the `lifespan()` function, after `ThrustCurveService.startup()`, check if `config.known_fliers_path` is set
    - If set, instantiate `FlierMatchService(known_fliers_path, flier_match_model)` and call `.load()`
    - Pass `flier_match_service` to `ExtractionService.__init__()`
    - Store on `app.state` for potential access elsewhere
    - _Requirements: 2.1, 2.4, 7.1, 7.5_

  - [ ] 4.2 Add `flier_match_service` parameter to `ExtractionService` and call in `_process()`
    - Add optional `flier_match_service` parameter to `ExtractionService.__init__()`
    - In `_process()`, after the ThrustCurve motor lookup block, add the flier verification step
    - Fetch record, extract membership from overflow, call `match_flier()`, then apply result
    - Implement `_apply_flier_match()` method: on match set `flier_name`, `flier_verified=True`, update overflow membership; on no match set status to `flier_not_found`; on error set status to `flier_match_failed`
    - Wrap in try/except: on any exception, log WARNING and leave status as `extracted`
    - _Requirements: 3.2, 3.3, 5.1, 5.2, 5.3, 5.4, 5.5, 6.1, 6.2, 6.3, 7.1, 7.4_

  - [ ]* 4.3 Write property test for successful match application (Property 5)
    - **Property 5: Successful match updates record correctly**
    - **Validates: Requirements 3.2, 5.1, 5.2, 5.3, 5.4**
    - For any valid line number L where 1 < L ≤ (row_count + 1), applying a match result should set `flier_verified=True`, update `flier_name` from the matched row, and update overflow membership data

  - [ ]* 4.4 Write property test for no-match preservation (Property 6)
    - **Property 6: No match preserves original data**
    - **Validates: Requirements 3.3, 6.1, 6.2, 6.3**
    - For any FlightRecord with existing data, when match result indicates no match (line_number=0), the record should retain original `flier_name` and overflow, `flier_verified` stays False, and status becomes `flier_not_found`

  - [ ]* 4.5 Write unit tests for FlierMatchService integration
    - Test end-to-end flow with a mocked Ollama response returning a valid line number
    - Test flow with mocked Ollama returning 0 (no match)
    - Test flow with mocked Ollama returning non-integer response
    - Test that flier matching is skipped when service is None or disabled
    - _Requirements: 7.1, 7.4, 7.5_

- [ ] 5. Final checkpoint
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- The feature is fully opt-in: when `known_fliers_path` is absent, no code paths change

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.3", "2.1"] },
    { "id": 1, "tasks": ["1.2", "2.2", "2.3", "2.6"] },
    { "id": 2, "tasks": ["2.4", "2.5", "2.7", "2.8"] },
    { "id": 3, "tasks": ["4.1", "4.2"] },
    { "id": 4, "tasks": ["4.3", "4.4", "4.5"] }
  ]
}
```
