# Bugfix Requirements Document

## Introduction

The property-based test file `tests/test_config_loading_fidelity.py` references `image_store_path` and `db_path` as independent, directly-settable top-level config JSON keys. However, `AppConfig` in `flight_card_scanner/config.py` has been refactored so that these are computed `@property` values derived from `event_data_path`. The config JSON only supports `event_data_path` as a key — there are no `image_store_path` or `db_path` keys recognized by `load_config`. This causes the tests to generate config dicts with keys that are silently ignored on load, and then assert against stale expectations that no longer match the actual behavior.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the `full_config_dicts` strategy generates a config dict THEN the system includes `image_store_path` and `db_path` as independent keys, which are not recognized by `load_config` and are silently ignored

1.2 WHEN `test_full_config_fields_match` asserts `result.image_store_path == Path(config_dict["image_store_path"])` THEN the system compares a computed property (`event_data_path / "images"`) against an unrelated arbitrary path string, causing assertion failures

1.3 WHEN `test_full_config_fields_match` asserts `result.db_path == Path(config_dict["db_path"])` THEN the system compares a computed property (`event_data_path / "flight_cards.db"`) against an unrelated arbitrary path string, causing assertion failures

1.4 WHEN the `partial_config_dicts` strategy omits `image_store_path` THEN the system asserts `result.image_store_path == Path("./images")`, but the actual default is `Path("./data") / "images"` (i.e., `Path("./data/images")`)

1.5 WHEN the `partial_config_dicts` strategy omits `db_path` THEN the system asserts `result.db_path == Path("./flight_cards.db")`, but the actual default is `Path("./data") / "flight_cards.db"` (i.e., `Path("./data/flight_cards.db")`)

1.6 WHEN `OPTIONAL_KEYS` lists `image_store_path` and `db_path` THEN the system treats non-existent config keys as valid optional keys, skewing the partial config generation

### Expected Behavior (Correct)

2.1 WHEN the `full_config_dicts` strategy generates a config dict THEN the system SHALL include `event_data_path` (as a path string) instead of `image_store_path` and `db_path`

2.2 WHEN `test_full_config_fields_match` asserts path correctness THEN the system SHALL verify `result.event_data_path == Path(config_dict["event_data_path"])` and that `result.image_store_path == result.event_data_path / "images"` and `result.db_path == result.event_data_path / "flight_cards.db"`

2.3 WHEN the `partial_config_dicts` strategy generates configs THEN the system SHALL use `event_data_path` as an optional key instead of `image_store_path` and `db_path`

2.4 WHEN `test_absent_keys_get_documented_defaults` checks defaults with `event_data_path` absent THEN the system SHALL assert `result.event_data_path == Path("./data")` and that the derived properties produce `Path("./data/images")` and `Path("./data/flight_cards.db")`

2.5 WHEN `OPTIONAL_KEYS` is defined THEN the system SHALL list `event_data_path` instead of `image_store_path` and `db_path`

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the config dict includes `host`, `port`, `event_name`, `event_date_range`, `extraction_mode`, or `extraction_endpoints` THEN the system SHALL CONTINUE TO test those keys with the same strategies and assertions as before

3.2 WHEN `test_absent_keys_get_documented_defaults` checks defaults for `host`, `port`, `event_name`, `event_date_range`, `extraction_mode`, and `extraction_endpoints` THEN the system SHALL CONTINUE TO assert the same documented default values

3.3 WHEN `load_config` parses a valid config file THEN the system SHALL CONTINUE TO return an `AppConfig` instance with all fields correctly populated

---

## Bug Condition

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type ConfigTestAssertion
  OUTPUT: boolean

  // Returns true when the test references image_store_path or db_path
  // as independent config keys rather than as derived properties of event_data_path
  RETURN X.references_key("image_store_path") AS independent config key
      OR X.references_key("db_path") AS independent config key
END FUNCTION
```

## Property Specification

```pascal
// Property: Fix Checking - Config keys match AppConfig structure
FOR ALL X WHERE isBugCondition(X) DO
  test_config ← generate_config_dict()
  ASSERT "event_data_path" IN test_config.keys()
  ASSERT "image_store_path" NOT IN test_config.keys()
  ASSERT "db_path" NOT IN test_config.keys()

  result ← load_config(test_config)
  ASSERT result.event_data_path = Path(test_config["event_data_path"])
  ASSERT result.image_store_path = result.event_data_path / "images"
  ASSERT result.db_path = result.event_data_path / "flight_cards.db"
END FOR
```

## Preservation Goal

```pascal
// Property: Preservation Checking
FOR ALL X WHERE NOT isBugCondition(X) DO
  // All other config keys (host, port, event_name, etc.) load identically
  ASSERT F(X) = F'(X)
END FOR
```
