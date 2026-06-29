# Implementation Plan: ThrustCurve DB Integration

## Overview

Replace the HTTP-based `ThrustCurveService` with a local `MotorLookupService` that loads motor data from the `thrustcurve-db` npm package JSON file into memory at startup. The implementation preserves the existing public interface so that `ExtractionService`, review router, and admin router continue working without changes to their calling code.

## Tasks

- [x] 1. Install thrustcurve-db package and create MotorLookupService
  - [x] 1.1 Add thrustcurve-db to package.json and install via pnpm
    - Add `"thrustcurve-db": "^2.0.0"` to the `dependencies` in the root `package.json`
    - Run `pnpm install` to install the package into `flight_card_scanner/static/js/node_modules/thrustcurve-db/`
    - Verify the JSON file exists at the expected path
    - _Requirements: 1.1, 1.2_

  - [x] 1.2 Create MotorLookupService module
    - Create `flight_card_scanner/services/motor_lookup_service.py`
    - Implement the `MotorLookupService` class with `__init__`, `startup`, `_load_database`, `_build_indexes` methods
    - Include the `_MANUFACTURER_ALIASES` dict as defined in the design
    - Implement `resolve_manufacturer`, `search_motors`, `get_motor_by_id` methods
    - Implement `lookup_motors` and `enrich_motors_for_display` async methods matching the existing interface
    - _Requirements: 2.1, 2.2, 2.3, 3.1, 3.2, 3.3, 3.4, 3.5, 4.1, 4.2, 4.3, 5.1, 5.2, 7.1, 7.2, 7.3, 8.1, 8.2, 8.3_

  - [ ]* 1.3 Write property test: Index Round-Trip Integrity
    - **Property 1: Index Round-Trip Integrity**
    - For any motor record present in the raw JSON, after startup indexing, querying `_by_common_name` with that motor's `commonName` (uppercased) returns a list containing that motor record
    - **Validates: Requirements 2.2**

  - [ ]* 1.4 Write property test: Search Result Correctness
    - **Property 2: Search Result Correctness**
    - For any search query, all returned motors have a `commonName` matching the query (case-insensitive), and if a manufacturer was specified and resolved, all results match that manufacturer
    - **Validates: Requirements 3.1, 3.2**

  - [ ]* 1.5 Write property test: Lookup Annotation Invariant
    - **Property 3: Lookup Annotation Invariant**
    - After `lookup_motors`, each motor has exactly one of: `thrustcurve_id`, `thrustcurve_candidates`, or `thrustcurve_error` — never more than one simultaneously
    - **Validates: Requirements 3.3, 3.4, 3.5, 8.1**

  - [ ]* 1.6 Write property test: Alias Resolution Case Insensitivity
    - **Property 4: Alias Resolution Case Insensitivity**
    - For any known alias, calling `resolve_manufacturer` with any case variation returns the same canonical abbreviation
    - **Validates: Requirements 4.1, 4.3**

  - [ ]* 1.7 Write property test: Enrichment Completeness
    - **Property 5: Enrichment Completeness**
    - For any motor with a valid `thrustcurve_id`, `enrich_motors_for_display` produces a `thrustcurve_data` dict containing all required fields
    - **Validates: Requirements 5.1, 8.2**

- [x] 2. Checkpoint - Verify MotorLookupService works standalone
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. Update configuration and remove old service
  - [x] 3.1 Remove thrustcurve_cache_path from AppConfig and load_config
    - Remove the `thrustcurve_cache_path` field from the `AppConfig` dataclass in `flight_card_scanner/config.py`
    - Remove the `thrustcurve_cache_path` parsing block from `load_config()` (the key will be naturally ignored since it is no longer read)
    - Remove `thrustcurve_cache_path` from the `return AppConfig(...)` call
    - Ensure any config.json with the old key still loads without error
    - _Requirements: 6.1, 6.2_

  - [ ]* 3.2 Write property test: Configuration Backward Compatibility
    - **Property 6: Configuration Backward Compatibility**
    - For any valid config JSON that includes a `thrustcurve_cache_path` key, `load_config` succeeds without error and the resulting `AppConfig` has no `thrustcurve_cache_path` attribute
    - **Validates: Requirements 6.2**

  - [x] 3.3 Delete ThrustCurveService module
    - Remove `flight_card_scanner/services/thrustcurve_service.py`
    - _Requirements: 7.1, 7.2, 7.3, 7.4_

- [x] 4. Wire MotorLookupService into the application
  - [x] 4.1 Update main.py lifespan to use MotorLookupService
    - Replace `from .services.thrustcurve_service import ThrustCurveService` with `from .services.motor_lookup_service import MotorLookupService`
    - Replace `ThrustCurveService(cache_dir=config.thrustcurve_cache_path)` instantiation with `MotorLookupService()`
    - Keep the `await ...startup()` call
    - Pass the new service instance as `thrustcurve_service=motor_lookup_service` to `ExtractionService` and `review.configure()`
    - Update `app.state.thrustcurve_service` to store the new service instance
    - Remove the `config.thrustcurve_cache_path` reference from `_log_config_summary`
    - _Requirements: 8.3, 8.4, 6.3_

  - [x] 4.2 Verify admin router compatibility
    - The admin router accesses `request.app.state.thrustcurve_service` — confirm the new service is stored on `app.state` with the same attribute name
    - Verify `lookup_motors` and `enrich_motors_for_display` are called the same way by the admin router
    - No code change should be needed since the interface is preserved
    - _Requirements: 8.1, 8.2_

- [x] 5. Checkpoint - Full integration verification
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Frontend accessibility and cleanup
  - [x] 6.1 Verify frontend access to thrustcurve-db JSON
    - Confirm the static file mount at `/static` serves `js/node_modules/thrustcurve-db/thrustcurve-db.json`
    - The existing `StaticFiles` mount in `main.py` already covers this path, so no code change is needed — just verify the file is accessible
    - _Requirements: 9.1, 9.2_

  - [ ]* 6.2 Write integration tests for MotorLookupService wiring
    - Test that startup loads the database without error
    - Test a known motor common name returns expected results via `lookup_motors`
    - Test that `enrich_motors_for_display` populates `thrustcurve_data` for a known motor ID
    - Test that a config.json with `thrustcurve_cache_path` key loads without error
    - _Requirements: 2.1, 3.1, 5.1, 6.2_

- [x] 7. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- The `MotorLookupService` is a drop-in replacement — it uses the same method names and return shapes as `ThrustCurveService`
- The admin router uses `request.app.state.thrustcurve_service` so storing the new service under the same attribute name requires no admin router changes
- The existing `pnpm` config in `package.json` directs `node_modules` to `flight_card_scanner/static/js/node_modules`, making the JSON accessible to both Python (file read) and the browser (static file serving)

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["1.3", "1.4", "1.5", "1.6", "1.7", "3.1"] },
    { "id": 3, "tasks": ["3.2", "3.3"] },
    { "id": 4, "tasks": ["4.1"] },
    { "id": 5, "tasks": ["4.2", "6.1"] },
    { "id": 6, "tasks": ["6.2"] }
  ]
}
```
