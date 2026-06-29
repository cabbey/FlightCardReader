# Implementation Plan

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Stale image_store_path/db_path Config Keys
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate the stale keys cause assertion failures
  - **Scoped PBT Approach**: Run the existing `test_full_config_fields_match` and `test_absent_keys_get_documented_defaults` tests against unfixed test code to confirm failures
  - Write a property-based test that generates a config dict with `event_data_path` (not `image_store_path`/`db_path`), loads it via `load_config`, and asserts:
    - `result.event_data_path == Path(config_dict["event_data_path"])`
    - `result.image_store_path == result.event_data_path / "images"`
    - `result.db_path == result.event_data_path / "flight_cards.db"`
  - Run test on UNFIXED code (the existing test file still uses stale keys)
  - **EXPECTED OUTCOME**: The existing tests fail because they reference `image_store_path` and `db_path` as independent config keys
  - Document counterexamples: e.g., `result.image_store_path` is `Path("./data/images")` but test expects `Path(config_dict["image_store_path"])`
  - Mark task complete when test is written, run, and failure is documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Non-Path Config Keys Unchanged
  - **IMPORTANT**: Follow observation-first methodology
  - Observe: Run existing tests for `host`, `port`, `event_name`, `event_date_range`, `extraction_mode`, `extraction_endpoints` on unfixed code — these should already pass
  - Write property-based test that generates config dicts with only non-path keys (`host`, `port`, `event_name`, `event_date_range`, `extraction_mode`, `extraction_endpoints`) and verifies:
    - `result.host == config_dict["host"]`
    - `result.port == config_dict["port"]`
    - `result.event_name == config_dict["event_name"]`
    - Date range start/end match
    - Extraction mode matches
    - Extraction endpoints match
  - Verify tests PASS on UNFIXED code (non-path assertions are correct in the existing test)
  - **EXPECTED OUTCOME**: Tests PASS (confirms baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3_

- [x] 3. Fix stale config keys in test_config_loading_fidelity.py

  - [x] 3.1 Replace `image_store_path` and `db_path` in `full_config_dicts` strategy with `event_data_path`
    - Remove `"image_store_path": draw(path_strings)` from the strategy
    - Remove `"db_path": draw(path_strings)` from the strategy
    - Add `"event_data_path": draw(path_strings)` to the strategy
    - _Bug_Condition: isBugCondition(X) where X.references_key("image_store_path") or X.references_key("db_path") as independent config keys_
    - _Expected_Behavior: Config dict uses event_data_path; derived properties verified via relationship_
    - _Preservation: host, port, event_name, event_date_range, extraction_mode, extraction_endpoints remain unchanged_
    - _Requirements: 2.1_

  - [x] 3.2 Update assertions in `test_full_config_fields_match`
    - Remove `assert result.image_store_path == Path(config_dict["image_store_path"])`
    - Remove `assert result.db_path == Path(config_dict["db_path"])`
    - Add `assert result.event_data_path == Path(config_dict["event_data_path"])`
    - Add `assert result.image_store_path == result.event_data_path / "images"`
    - Add `assert result.db_path == result.event_data_path / "flight_cards.db"`
    - _Bug_Condition: isBugCondition(X) where assertions compare derived properties against config dict keys_
    - _Expected_Behavior: Assertions verify event_data_path loaded correctly and derived properties match_
    - _Preservation: All other assertions (host, port, event_name, date range, endpoints) unchanged_
    - _Requirements: 2.2_

  - [x] 3.3 Update `OPTIONAL_KEYS` list
    - Remove `"image_store_path"` and `"db_path"` from the list
    - Add `"event_data_path"` to the list
    - _Bug_Condition: isBugCondition(X) where optional_keys_list_contains("image_store_path") or ("db_path")_
    - _Expected_Behavior: OPTIONAL_KEYS lists event_data_path instead of stale keys_
    - _Preservation: All other keys in OPTIONAL_KEYS remain unchanged_
    - _Requirements: 2.5_

  - [x] 3.4 Update `partial_config_dicts` strategy
    - Remove `if "image_store_path" in included: config["image_store_path"] = draw(path_strings)` block
    - Remove `if "db_path" in included: config["db_path"] = draw(path_strings)` block
    - Add `if "event_data_path" in included: config["event_data_path"] = draw(path_strings)` block
    - _Bug_Condition: isBugCondition(X) where strategy generates stale keys_
    - _Expected_Behavior: Strategy generates event_data_path when included_
    - _Preservation: All other key generation blocks unchanged_
    - _Requirements: 2.3_

  - [x] 3.5 Update default assertions in `test_absent_keys_get_documented_defaults`
    - Remove the `if "image_store_path" not in config_dict` block (asserts `Path("./images")`)
    - Remove the `if "db_path" not in config_dict` block (asserts `Path("./flight_cards.db")`)
    - Add `if "event_data_path" not in config_dict` block that asserts:
      - `result.event_data_path == Path("./data")`
      - `result.image_store_path == Path("./data/images")`
      - `result.db_path == Path("./data/flight_cards.db")`
    - Add `else` block that asserts:
      - `result.event_data_path == Path(config_dict["event_data_path"])`
      - `result.image_store_path == result.event_data_path / "images"`
      - `result.db_path == result.event_data_path / "flight_cards.db"`
    - _Bug_Condition: isBugCondition(X) where defaults assert stale paths_
    - _Expected_Behavior: Defaults verify event_data_path="./data" and derived properties_
    - _Preservation: All other default assertions unchanged_
    - _Requirements: 2.4_

  - [x] 3.6 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Stale Keys Replaced with event_data_path
    - **IMPORTANT**: Re-run the SAME test from task 1 - do NOT write a new test
    - The test from task 1 encodes the expected behavior
    - When this test passes, it confirms the expected behavior is satisfied
    - Run bug condition exploration test from step 1
    - **EXPECTED OUTCOME**: Test PASSES (confirms bug is fixed)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

  - [x] 3.7 Verify preservation tests still pass
    - **Property 2: Preservation** - Non-Path Config Keys Unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run preservation property tests from step 2
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - Confirm all tests still pass after fix (no regressions)

- [x] 4. Checkpoint - Ensure all tests pass
  - Run the full test suite: `.venv/bin/python -m pytest tests/test_config_loading_fidelity.py -v`
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Update README.md documentation for current config schema
  - Update the example `config.json` block in the Configuration section:
    - Remove `"image_store_path": "./images"` and `"db_path": "./flight_cards.db"` entries
    - Add `"event_data_path": "./data"` entry
    - Add `"thrustcurve_cache_path": "./thrustcurve_cache"` entry
  - Update the Configuration Keys table:
    - Remove the `image_store_path` row
    - Remove the `db_path` row
    - Add `event_data_path` row: type string, default `"./data"`, description "Base directory for event data. Images are stored in `<event_data_path>/images/` and the database at `<event_data_path>/flight_cards.db`."
    - Add `thrustcurve_cache_path` row: type string, default `"./thrustcurve_cache"`, description "Directory for caching ThrustCurve.org motor data."
    - Add `known_fliers_path` row: type string, default *(none)*, description "Path to a TSV file of known fliers for post-extraction name verification. Requires `flier_match_model` to also be set."
    - Add `flier_match_model` row: type string, default *(none)*, description "Ollama model name used for flier name matching (e.g., `qwen2.5:7b`). Required when `known_fliers_path` is set."
  - _Requirements: Documentation accuracy for config schema_
