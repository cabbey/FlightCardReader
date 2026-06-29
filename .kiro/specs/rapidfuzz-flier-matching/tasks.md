# Implementation Plan: Rapidfuzz Flier Matching

## Overview

Replace the LLM-based flier matching with local rapidfuzz-based fuzzy string matching. The FlierMatchService, config, main.py wiring, call site, and tests are already complete. The remaining work is rewriting `_apply_flier_match()` in ExtractionService to use the unified roster data import pattern (both tiers apply the same data, differing only in `flier_verified` and `flier_match_status`), and adding integration tests for that logic.

## Tasks

- [x] 1. Rewrite FlierMatchService with rapidfuzz
  - [x] 1.1 Replace FlierMatchService implementation
    - Remove all LLM/httpx/prompt logic
    - Implement `FlierMatchResult` with `confidence` field
    - Implement `_normalize_name()`, `_compute_name_similarity()` using `rapidfuzz.fuzz.WRatio`
    - Implement `_find_rows_by_member_number()` with three-case logic (club specified found, club specified fallback, no club)
    - Implement `_score_candidate()` with composite scoring and confidence calculation
    - Implement `match_flier()` async method without `client` parameter
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4, 2.5, 3.1, 3.2, 3.3, 4.1, 4.2, 4.3, 4.4, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 7.1, 7.2, 7.3, 7.4_

- [x] 2. Update configuration
  - [x] 2.1 Update AppConfig and load_config
    - Add `auto_accept_threshold: float = 0.95` to `AppConfig`
    - Remove requirement that `flier_match_model` must be present when `known_fliers_path` is set
    - Load `auto_accept_threshold` from config JSON with default of 0.95
    - _Requirements: 6.1, 6.2, 6.3, 9.1, 9.7_

- [x] 3. Update main.py construction
  - [x] 3.1 Remove model parameter from FlierMatchService construction
    - Change constructor call to `FlierMatchService(known_fliers_path=config.known_fliers_path)`
    - _Requirements: 5.6_

- [x] 4. Update ExtractionService call site
  - [x] 4.1 Update `_process()` to call `match_flier()` without client
    - Pass `flier_name=`, `club=`, `member_number=`, `cert_level=` as kwargs
    - Remove `client` argument from the call
    - _Requirements: 8.1, 8.2_

- [x] 5. Rewrite ExtractionService `_apply_flier_match()` logic
  - [x] 5.1 Rewrite `_apply_flier_match()` to use unified roster data import
    - Both high-confidence and low-confidence tiers apply the SAME data import from the roster row
    - Set `record.flier_name` = roster row's `Name` (for both tiers)
    - Set `overflow["membership"]` = `{"nar_number": row.get("NAR") or None, "tra_number": row.get("TRA") or None, "cert_level": int(row["Level"]) if row.get("Level") else None}`
    - Set `overflow["flier_match_confidence"]` = `result.confidence`
    - High confidence (`confidence > auto_accept_threshold`): set `record.flier_verified = True`, `overflow["flier_match_status"] = "verified"`
    - Low confidence (`confidence <= auto_accept_threshold`): set `record.flier_verified = False`, `overflow["flier_match_status"] = "review"`
    - Remove all `flier_match_candidate` logic — no candidate object stored for either tier
    - Remove the old single-club format (`{"club": "NAR", "member_number": "12345"}`) from the high-confidence path
    - _Requirements: 8.3, 9.2, 9.3, 9.4, 9.5, 9.6, 9.8_

  - [ ]* 5.2 Write integration tests for `_apply_flier_match()`
    - **Property 10: Auto-Accept Tiered Behavior**
    - **Validates: Requirements 9.2, 9.3, 9.4, 9.5, 9.6, 9.8**
    - Test high-confidence match sets `flier_verified=True` and `flier_match_status="verified"`
    - Test low-confidence match sets `flier_verified=False` and `flier_match_status="review"`
    - Test both tiers produce the same `overflow["membership"]` format with `nar_number`, `tra_number`, `cert_level`
    - Test both tiers set `record.flier_name` to roster name
    - Test no `flier_match_candidate` key exists in overflow for either tier
    - Test error case sets `flier_match_status="error"`
    - Test no-match case sets `flier_match_status="not_found"`
    - _Requirements: 9.2, 9.3, 9.4, 9.5, 9.6, 9.8_

- [x] 6. Final checkpoint
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Rewrite flier match tests
  - [x] 7.1 Rewrite test_flier_match_flow.py
    - Update tests for the new rapidfuzz-based interface
    - _Requirements: 1.1, 1.2, 2.1, 2.3, 4.1_

  - [x] 7.2 Rewrite test_flier_match_parse.py
    - Update tests for internal helper methods
    - _Requirements: 1.1, 2.1, 2.2, 2.3_

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- The FlierMatchService rewrite, config changes, main.py wiring, call site update, and test rewrites are already complete
- The only remaining implementation work is task 5.1 (rewriting `_apply_flier_match()`) and its integration tests (5.2)
- Property tests validate the universal correctness property for tiered auto-accept behavior
- The key behavioral change: both tiers now apply identical roster data import; the old code stored a separate `flier_match_candidate` for low-confidence matches and used a single-club format for high-confidence

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["5.1"] },
    { "id": 1, "tasks": ["5.2"] }
  ]
}
```
