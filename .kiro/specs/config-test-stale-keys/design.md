# Config Test Stale Keys Bugfix Design

## Overview

The property-based test file `tests/test_config_loading_fidelity.py` references `image_store_path` and `db_path` as independent, directly-settable top-level config JSON keys. After a refactor, these are now computed `@property` values derived from `event_data_path` in `AppConfig`. The tests generate config dicts with keys that `load_config` silently ignores, then assert against stale expectations. The fix updates the test file to use `event_data_path` as the config key and verify the derived properties correctly.

## Glossary

- **Bug_Condition (C)**: Any test assertion or strategy that references `image_store_path` or `db_path` as independent config JSON keys that can be set directly in the config file
- **Property (P)**: Tests should use `event_data_path` as the config key and verify that `image_store_path` and `db_path` are correctly derived as `event_data_path / "images"` and `event_data_path / "flight_cards.db"` respectively
- **Preservation**: All other test logic for `host`, `port`, `event_name`, `event_date_range`, `extraction_mode`, `extraction_endpoints`, and general test structure must remain unchanged
- **`load_config`**: The function in `flight_card_scanner/config.py` that parses a JSON config file into an `AppConfig` instance
- **`AppConfig`**: The dataclass in `flight_card_scanner/config.py` holding all application configuration; `event_data_path` is a settable field, while `image_store_path` and `db_path` are computed properties
- **`event_data_path`**: A `Path` field on `AppConfig` defaulting to `Path("./data")`; serves as the base directory for images and the database file

## Bug Details

### Bug Condition

The bug manifests when the test file generates config dicts containing `image_store_path` and `db_path` as top-level keys, or when assertions compare these properties against values drawn from config dict keys that `load_config` does not recognize. The `load_config` function only reads `event_data_path` from JSON — it never reads `image_store_path` or `db_path` keys.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type TestAssertion
  OUTPUT: boolean

  RETURN input.config_dict_contains_key("image_store_path")
         OR input.config_dict_contains_key("db_path")
         OR input.asserts_property_equals_config_key("image_store_path")
         OR input.asserts_property_equals_config_key("db_path")
         OR input.optional_keys_list_contains("image_store_path")
         OR input.optional_keys_list_contains("db_path")
         OR input.asserts_default("image_store_path", Path("./images"))
         OR input.asserts_default("db_path", Path("./flight_cards.db"))
END FUNCTION
```

### Examples

- **Full config generation**: `full_config_dicts()` generates `{"image_store_path": "/some/path", "db_path": "/other/path", ...}` — these keys are ignored by `load_config`, so assertions comparing `result.image_store_path` to `/some/path` fail
- **Full config assertion**: `assert result.image_store_path == Path(config_dict["image_store_path"])` — actual value is `event_data_path / "images"`, not the arbitrary string from the dict
- **Partial config defaults**: When `image_store_path` is absent, test asserts `result.image_store_path == Path("./images")` — actual default is `Path("./data/images")`
- **Partial config defaults**: When `db_path` is absent, test asserts `result.db_path == Path("./flight_cards.db")` — actual default is `Path("./data/flight_cards.db")`
- **OPTIONAL_KEYS list**: Contains `"image_store_path"` and `"db_path"` which are not valid config file keys

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Test strategies and assertions for `host`, `port`, `event_name`, `event_date_range`, `extraction_mode`, and `extraction_endpoints` must remain identical
- The `full_config_dicts` strategy must continue to generate valid config dicts for all other keys
- The `partial_config_dicts` strategy must continue to randomly omit optional keys and test defaults for all other keys
- The `TestConfigLoadingFidelity` class structure and property-based test approach must remain unchanged
- The Hypothesis `@settings(max_examples=50)` configuration must remain unchanged

**Scope:**
All assertions and strategies that do NOT involve `image_store_path` or `db_path` as independent config keys should be completely unaffected by this fix. This includes:
- Host/port generation and assertion logic
- Event name generation and assertion logic
- Date range generation, parsing, and assertion logic
- Extraction mode sampling and assertion logic
- Endpoint list generation and assertion logic

## Hypothesized Root Cause

Based on the bug description, the issue is straightforward:

1. **Stale test strategy (`full_config_dicts`)**: The strategy was written when `image_store_path` and `db_path` were independent config keys. After the refactor to computed properties, the strategy was not updated to use `event_data_path` instead.

2. **Stale assertions (`test_full_config_fields_match`)**: The assertions `result.image_store_path == Path(config_dict["image_store_path"])` and `result.db_path == Path(config_dict["db_path"])` assume these values come from the config dict, but they are now derived from `event_data_path`.

3. **Stale OPTIONAL_KEYS list**: The list includes `"image_store_path"` and `"db_path"` which are no longer valid config file keys. It should include `"event_data_path"` instead.

4. **Stale default assertions (`test_absent_keys_get_documented_defaults`)**: The defaults `Path("./images")` and `Path("./flight_cards.db")` are wrong — the actual defaults are `Path("./data/images")` and `Path("./data/flight_cards.db")` (derived from the default `event_data_path` of `Path("./data")`).

## Correctness Properties

Property 1: Bug Condition - Config dict uses event_data_path and derived properties are verified

_For any_ config dict generated by the test strategy that includes path configuration, the test SHALL use `event_data_path` as the config key, and assertions SHALL verify that `result.event_data_path == Path(config_dict["event_data_path"])`, `result.image_store_path == result.event_data_path / "images"`, and `result.db_path == result.event_data_path / "flight_cards.db"`.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**

Property 2: Preservation - Non-path config keys behave identically

_For any_ config dict generated by the test strategy where only non-path keys (`host`, `port`, `event_name`, `event_date_range`, `extraction_mode`, `extraction_endpoints`) are tested, the fixed tests SHALL produce exactly the same assertions and use the same strategies as the original tests, preserving all existing test coverage for these keys.

**Validates: Requirements 3.1, 3.2, 3.3**

## Fix Implementation

### Changes Required

Assuming our root cause analysis is correct:

**File**: `tests/test_config_loading_fidelity.py`

**Specific Changes**:

1. **Replace `image_store_path` and `db_path` in `full_config_dicts` strategy**: Remove these two keys from the generated dict and add `"event_data_path": draw(path_strings)` instead.

2. **Update assertions in `test_full_config_fields_match`**: Replace:
   - `assert result.image_store_path == Path(config_dict["image_store_path"])` 
   - `assert result.db_path == Path(config_dict["db_path"])`
   
   With:
   - `assert result.event_data_path == Path(config_dict["event_data_path"])`
   - `assert result.image_store_path == result.event_data_path / "images"`
   - `assert result.db_path == result.event_data_path / "flight_cards.db"`

3. **Update `OPTIONAL_KEYS` list**: Replace `"image_store_path"` and `"db_path"` with `"event_data_path"`.

4. **Update `partial_config_dicts` strategy**: Replace the `if "image_store_path" in included` and `if "db_path" in included` blocks with `if "event_data_path" in included: config["event_data_path"] = draw(path_strings)`.

5. **Update default assertions in `test_absent_keys_get_documented_defaults`**: Replace:
   - `if "image_store_path" not in config_dict: assert result.image_store_path == Path("./images")`
   - `if "db_path" not in config_dict: assert result.db_path == Path("./flight_cards.db")`
   
   With:
   - `if "event_data_path" not in config_dict: assert result.event_data_path == Path("./data")` and verify derived properties `assert result.image_store_path == Path("./data/images")` and `assert result.db_path == Path("./data/flight_cards.db")`
   - `else: assert result.event_data_path == Path(config_dict["event_data_path"])` and verify derived properties against the provided path

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bug on unfixed code, then verify the fix works correctly and preserves existing behavior.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix. Confirm or refute the root cause analysis. If we refute, we will need to re-hypothesize.

**Test Plan**: Run the existing `test_config_loading_fidelity.py` tests against the current `AppConfig` to observe assertion failures caused by the stale keys.

**Test Cases**:
1. **Full config with image_store_path**: Generate a config dict with `image_store_path` set to an arbitrary path — `load_config` ignores it, assertion fails (will fail on unfixed code)
2. **Full config with db_path**: Generate a config dict with `db_path` set to an arbitrary path — `load_config` ignores it, assertion fails (will fail on unfixed code)
3. **Partial config missing image_store_path**: Omit `image_store_path`, assert default `Path("./images")` — actual is `Path("./data/images")`, assertion fails (will fail on unfixed code)
4. **Partial config missing db_path**: Omit `db_path`, assert default `Path("./flight_cards.db")` — actual is `Path("./data/flight_cards.db")`, assertion fails (will fail on unfixed code)

**Expected Counterexamples**:
- `result.image_store_path` is `Path("./data/images")` but test expects `Path(config_dict["image_store_path"])` or `Path("./images")`
- `result.db_path` is `Path("./data/flight_cards.db")` but test expects `Path(config_dict["db_path"])` or `Path("./flight_cards.db")`

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed tests correctly validate the relationship between `event_data_path` and the derived properties.

**Pseudocode:**
```
FOR ALL config_dict WHERE "event_data_path" IN config_dict DO
  result := load_config(config_dict)
  ASSERT result.event_data_path = Path(config_dict["event_data_path"])
  ASSERT result.image_store_path = result.event_data_path / "images"
  ASSERT result.db_path = result.event_data_path / "flight_cards.db"
END FOR

FOR ALL config_dict WHERE "event_data_path" NOT IN config_dict DO
  result := load_config(config_dict)
  ASSERT result.event_data_path = Path("./data")
  ASSERT result.image_store_path = Path("./data/images")
  ASSERT result.db_path = Path("./data/flight_cards.db")
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed tests produce the same assertions as the original tests for non-path keys.

**Pseudocode:**
```
FOR ALL config_dict WHERE NOT isBugCondition(assertion) DO
  ASSERT fixed_test_assertions(config_dict, key) = original_test_assertions(config_dict, key)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many test cases automatically across the input domain
- It catches edge cases that manual unit tests might miss
- It provides strong guarantees that behavior is unchanged for all non-buggy inputs

**Test Plan**: Run the fixed tests and verify that all non-path-related assertions pass identically to how they would have passed before the refactor (host, port, event_name, etc.).

**Test Cases**:
1. **Host preservation**: Verify that host generation, loading, and assertion logic works identically after the fix
2. **Port preservation**: Verify that port generation, loading, and assertion logic works identically after the fix
3. **Event name preservation**: Verify that event_name generation, loading, and default assertion logic works identically
4. **Date range preservation**: Verify that event_date_range generation, parsing, and assertion logic works identically
5. **Extraction mode preservation**: Verify that extraction_mode sampling and assertion logic works identically
6. **Endpoints preservation**: Verify that extraction_endpoints generation, loading, and assertion logic works identically

### Unit Tests

- Run `test_full_config_fields_match` with the fixed strategy to verify `event_data_path` is loaded and derived properties are correct
- Run `test_absent_keys_get_documented_defaults` with the fixed OPTIONAL_KEYS to verify correct defaults for `event_data_path` and derived properties
- Verify that config dicts no longer contain `image_store_path` or `db_path` as keys

### Property-Based Tests

- Generate random `event_data_path` values and verify `image_store_path` always equals `event_data_path / "images"`
- Generate random `event_data_path` values and verify `db_path` always equals `event_data_path / "flight_cards.db"`
- Generate configs with random subsets of optional keys omitted and verify all defaults are correct

### Integration Tests

- Run the full test suite to confirm no regressions in other test files that may depend on config loading
- Verify that `load_config` with a real config file produces correct `AppConfig` with derived paths
