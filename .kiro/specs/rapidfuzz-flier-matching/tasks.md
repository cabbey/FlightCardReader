# Implementation Plan: Rapidfuzz Flier Matching

## Overview

Replace the LLM-based flier matching in `FlierMatchService` with local `rapidfuzz`-based fuzzy string matching. Update configuration to remove the `flier_match_model` requirement and add `auto_accept_threshold`. Update the ExtractionService caller to use the new interface with tiered auto-accept logic. Rewrite tests to cover the new behavior.

## Tasks

- [x] 1. Update configuration and add `auto_accept_threshold`
  - [x] 1.1 Modify `flight_card_scanner/config.py` to add `auto_accept_threshold` field and relax validation
    - Add `auto_accept_threshold: float = 0.95` field to `AppConfig` dataclass
    - In `load_config()`, read `auto_accept_threshold` from config JSON with default 0.95
    - Remove the validation that raises `ConfigError` when `known_fliers_path` is set but `flier_match_model` is absent
    - Keep `flier_match_model` field as `str | None` for backward compatibility (no longer validated)
    - _Requirements: 6.1, 6.2, 6.3, 9.6_

  - [ ]* 1.2 Write property test for config accepts known_fliers without model
    - **Property 9: Config Accepts Known Fliers Without Model**
    - **Validates: Requirements 6.1, 6.3**

- [x] 2. Rewrite `FlierMatchService` with rapidfuzz
  - [x] 2.1 Rewrite `flight_card_scanner/services/flier_match_service.py` with rapidfuzz matching
    - Remove all LLM-related code: `build_prompt()`, `build_payload()`, `parse_response()`, `get_row_by_line_number()`, httpx import, model storage
    - Add `confidence: float` field to `FlierMatchResult` dataclass
    - Update constructor: remove `flier_match_model` parameter, add `name_only_threshold` and `member_confirmed_threshold` keyword args with defaults (80.0 and 60.0)
    - Implement `_normalize_name()` static method (strip + lowercase)
    - Implement `_compute_name_similarity()` using `rapidfuzz.fuzz.WRatio`
    - Implement `_find_rows_by_member_number()` with three-case logic (club specified found, club specified fallback to other, no club searches both columns)
    - Implement `_score_candidate()` returning (composite_score, confidence) tuple
    - Implement `match_flier()` async method without `client` parameter: scores all roster rows, applies thresholds, returns best match
    - Keep `load()`, `enabled`, `row_count` unchanged in behavior
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4, 2.5, 3.1, 3.2, 3.3, 4.1, 4.2, 4.3, 4.4, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 7.1, 7.2, 7.3, 7.4_

  - [ ]* 2.2 Write property test for best match optimality
    - **Property 1: Best Match Optimality**
    - **Validates: Requirements 1.2, 1.4**

  - [ ]* 2.3 Write property test for threshold gating
    - **Property 2: Threshold Gating**
    - **Validates: Requirements 1.3, 3.1, 3.2**

  - [ ]* 2.4 Write property test for member number lookup completeness
    - **Property 3: Member Number Lookup Completeness**
    - **Validates: Requirements 2.1, 2.2, 2.3**

  - [ ]* 2.5 Write property test for member-confirmed name verification
    - **Property 4: Member-Confirmed Name Verification**
    - **Validates: Requirements 2.4**

  - [ ]* 2.6 Write property test for duplicate member number disambiguation
    - **Property 5: Duplicate Member Number Disambiguation**
    - **Validates: Requirements 2.5**

  - [ ]* 2.7 Write property test for cert level independence
    - **Property 6: Cert Level Independence**
    - **Validates: Requirements 3.3**

  - [ ]* 2.8 Write property test for confidence score invariant
    - **Property 7: Confidence Score Invariant**
    - **Validates: Requirements 4.3, 4.4**

  - [ ]* 2.9 Write property test for load reflects TSV content
    - **Property 8: Load Reflects TSV Content**
    - **Validates: Requirements 5.2, 5.3**

- [x] 3. Checkpoint - Ensure FlierMatchService tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Update ExtractionService caller and main.py
  - [x] 4.1 Update `flight_card_scanner/main.py` to remove `flier_match_model` from FlierMatchService construction
    - Change `FlierMatchService(known_fliers_path=..., flier_match_model=...)` to `FlierMatchService(known_fliers_path=...)`
    - _Requirements: 5.6_

  - [x] 4.2 Update `flight_card_scanner/services/extraction_service.py` with new call site and tiered auto-accept logic
    - Store `auto_accept_threshold` from config in `__init__`
    - Remove `client` argument from the `match_flier()` call site in `_process()`
    - Rewrite `_apply_flier_match()` with three branches:
      - Error/not_found: store status in overflow only
      - High confidence (> auto_accept_threshold): set `flier_verified=True`, `flier_match_status="verified"`, apply row data to record fields
      - Lower confidence (matched but <= auto_accept_threshold): set `flier_verified=False`, `flier_match_status="review"`, store candidate in `overflow["flier_match_candidate"]` WITHOUT overwriting existing `flier_name` or membership fields
    - Always store `flier_match_confidence` in overflow when matched
    - _Requirements: 8.1, 8.2, 8.3, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6_

  - [ ]* 4.3 Write property test for auto-accept tiered behavior
    - **Property 10: Auto-Accept Tiered Behavior**
    - **Validates: Requirements 9.2, 9.3, 9.4, 9.5**

- [x] 5. Rewrite test files for new interface
  - [x] 5.1 Rewrite `tests/test_flier_match_flow.py` for new rapidfuzz-based FlierMatchService
    - Remove all httpx/mock-response machinery
    - Test `match_flier()` directly against loaded TSV (no network mocking needed)
    - Test cases: exact name match, close name match, member number confirmation with lower name sim, no-club-with-number scenario, below-threshold returns no match, disabled service returns no match
    - Verify confidence scores are in [0.0, 1.0] and match the expected tiering
    - _Requirements: 1.1, 1.2, 1.3, 2.1, 2.3, 4.1, 4.4, 5.4, 5.5_

  - [x] 5.2 Rewrite `tests/test_flier_match_parse.py` to test rapidfuzz-specific behavior
    - Remove tests for `parse_response()` and `build_prompt()` (those methods no longer exist)
    - Replace with tests for: `_normalize_name()`, `_compute_name_similarity()`, `_find_rows_by_member_number()`, `_score_candidate()`
    - Test no-club-with-number as a primary pattern (member number found in either column without club hint)
    - Test auto-accept vs review tiering by verifying confidence values against threshold
    - _Requirements: 1.1, 2.1, 2.2, 2.3, 4.1, 4.2_

  - [x] 5.3 Write integration tests for ExtractionService `_apply_flier_match()` tiered logic
    - Test high-confidence result triggers auto-accept path (flier_verified=True, status="verified", row data applied)
    - Test lower-confidence result triggers review path (flier_verified=False, status="review", candidate stored in overflow, existing fields preserved)
    - Test error result stores "error" status
    - Test not_found result stores "not_found" status
    - _Requirements: 9.2, 9.3, 9.4, 9.5_

- [x] 6. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- The project uses `rapidfuzz` which must be installed: `.venv/bin/pip install rapidfuzz`
- All test commands: `.venv/bin/python -m pytest tests/ -v`
- The no-club-with-number scenario is a primary use case (not a fallback) and must be tested prominently

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "2.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "2.4", "2.5", "2.6", "2.7", "2.8", "2.9"] },
    { "id": 3, "tasks": ["4.1", "4.2"] },
    { "id": 4, "tasks": ["4.3", "5.1", "5.2"] },
    { "id": 5, "tasks": ["5.3"] }
  ]
}
```
