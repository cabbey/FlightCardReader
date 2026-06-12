# Implementation Plan: Flight Card Scanner

## Overview

A local-network web application that digitises handwritten rocket-launch flight cards. The implementation is split into: (1) project scaffolding and configuration, (2) database and data models, (3) server-side services and routing, (4) extraction pipeline and Ollama integration, (5) Jinja2 review UI, and (6) browser-side OpenCV.js scanning client. Each phase wires into the previous ones so nothing is left orphaned.

## Tasks

- [x] 1. Set up project structure and configuration

  - Create the `flight_card_scanner/` package directory layout matching the component map in the design (`routers/`, `services/`, `templates/`, `static/js/`)
  - Create `package.json` declaring `opencv.js` as a dependency, and add `pnpm-lock.yaml`; configure `static/js/` as the pnpm output directory
  - Create `config.py` with `EndpointConfig`, `DateRange`, and `AppConfig` dataclasses plus the `load_config(path)` function; log defaults for absent keys; raise `ConfigError` on invalid values
  - Create a sample `config.json` in the project root with all required fields
  - Create `exceptions.py` defining the full exception hierarchy: `FlightCardScannerError`, `ConfigError`, `ImageStorageError`, `ExtractionParseError`, `OllamaUnavailableError`, `DateResolutionError`
  - _Requirements: 9.1, 9.2, 9.3, 9.7, 11.1, 11.2, 11.4_

  - [x] 1.1 Write property test for config loading fidelity
    - **Property 19: Config loading fidelity**
    - Generate arbitrary valid config dicts with all keys present and assert `load_config` returns matching `AppConfig` fields; also generate configs with optional keys absent and assert documented defaults are applied
    - **Validates: Requirements 9.2, 9.3**

- [x] 2. Implement database layer and ORM models

  - [x] 2.1 Create `database.py` with SQLAlchemy async engine, session factory, and `Base`
    - Use `aiosqlite` driver; expose `get_db` async dependency and `create_all` helper
    - _Requirements: 6.1, 6.9_

  - [x] 2.2 Create `models.py` with the `FlightRecord` ORM class
    - Include all dedicated columns (`id`, `created_at`, `image_path`, `extraction_status`, `flight_date`, `flier_name`, `total_impulse_value`, `total_impulse_unit`, `flag_heads_up`, `flag_first_flight`, `flag_complex`, `rack`, `pad`, `fso_rso_initials`, `evaluation_outcome`, `evaluation_comments`, `overflow`) matching the design schema
    - Add indexes on `extraction_status` and `created_at DESC`
    - _Requirements: 6.2, 6.3_

  - [x] 2.3 Create `schemas.py` with all Pydantic models
    - `MembershipInfo`, `RocketMeasurements`, `MotorEntry`, `FlightCardExtraction` (with `format`-compatible JSON Schema), `ScanResponse`, `SetModeRequest`, `ModeResponse`, `TriggerResponse`, `RequeueResponse`, `FlightRecordSummary`, `FlightRecordDetail`
    - _Requirements: 5.3, 5.5_

- [x] 3. Implement service layer

  - [x] 3.1 Create `services/image_service.py`
    - Implement `save_image(file_bytes, ext, store_path) -> str` that writes the file to a UUID4-based filename in the Image Store and returns the relative path
    - Implement `delete_image(path)` for rollback on DB failure
    - Raise `ImageStorageError` if the directory is not writable
    - _Requirements: 4.1, 4.2, 4.5, 4.6, 10.1, 10.2_

  - [ ]* 3.2 Write property test for image round-trip fidelity
    - **Property 1: Image round-trip fidelity**
    - Generate arbitrary byte sequences, save via `save_image`, read back via the static path, and assert byte-for-byte equality
    - **Validates: Requirements 10.1, 10.2**

  - [x] 3.3 Create `services/record_service.py`
    - Implement `create(db, image_path) -> FlightRecord` (status = `pending`)
    - Implement `get(db, record_id)`, `get_by_status(db, status)`, `set_status(db, record_id, status)`
    - Implement `apply_extraction(db, record_id, extracted: FlightCardExtraction, resolved_date)` that maps all schema fields to dedicated columns and overflow JSON
    - _Requirements: 4.2, 4.3, 6.4, 6.5, 6.6, 6.7, 6.8_

  - [ ]* 3.4 Write property test for extraction result persistence
    - **Property 6: Extraction result persistence — valid and partial responses**
    - Generate arbitrary `FlightCardExtraction` instances (including all-null), call `apply_extraction`, re-read the record, and assert every non-null field is present in the expected column or overflow key, and `extraction_status = "extracted"`
    - **Validates: Requirements 5.4, 5.5, 5.6**

  - [ ]* 3.5 Write property test for invalid LLM response triggers extraction_failed
    - **Property 7: Invalid LLM response triggers extraction_failed**
    - Generate arbitrary byte strings that fail `FlightCardExtraction.model_validate_json`, invoke the parse path, and assert the record is set to `extraction_failed` with no field values written
    - **Validates: Requirements 5.5, 5.7**

  - [x] 3.6 Implement `resolve_flight_date` in `services/extraction_service.py`
    - Handle: `None`/empty → `None`; day-of-week names (full + abbreviated, case-insensitive) → resolve within `DateRange`; numeric / ISO strings → parse + validate in range
    - Raise `DateResolutionError` for unresolvable values
    - _Requirements: 5.10, 5.11, 5.12_

  - [x] 3.7 Write property test for day-of-week date resolution
    - **Property 8: Day-of-week date resolution**
    - For every day name in the full/abbreviated set and a generated date range containing that weekday, assert `resolve_flight_date` returns the unique matching calendar date
    - **Validates: Requirements 5.10, 5.11**

  - [x] 3.8 Write property test for out-of-range date consequence chain
    - **Property 9: Out-of-range date — full failure consequence chain**
    - Generate dates provably outside the event date range, run through `apply_extraction` + `resolve_flight_date`, and assert `flight_date = null`, `overflow['raw_flight_date'] = raw_string`, `extraction_status = "extraction_failed"` atomically
    - **Validates: Requirements 5.11, 5.12**

- [x] 4. Checkpoint — ensure core services pass all tests
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Implement the extraction worker pool

  - [x] 5.1 Implement `ExtractionService` class in `services/extraction_service.py`
    - `__init__`: initialise `_mode`, `asyncio.Queue`, per-endpoint `asyncio.Semaphore` dict, worker list
    - `start()`: spawn one worker `asyncio.Task` per endpoint concurrency slot
    - `stop()`: `queue.join()` with 30 s timeout, then cancel workers
    - `enqueue(record_id)`: puts only in `IMMEDIATE` mode
    - `set_mode(mode)`: switches mode; if `DEFERRED → IMMEDIATE`, calls `trigger_pending`
    - `trigger_pending()`: fetches all `pending` records and enqueues them; returns count
    - `_worker(endpoint, sem)`: infinite loop — get ID, acquire sem, call `_process`, release, `task_done`
    - `_process(record_id, client, endpoint_url)`: set `processing`, call Ollama, apply extraction or set `extraction_failed`
    - _Requirements: 5.1, 5.2, 12.2, 12.3, 12.7, 12.8, 13.2, 13.3, 13.4_

  - [ ]* 5.2 Write property test for mode controls dispatch of new records
    - **Property 12: Mode controls dispatch of new records**
    - In `IMMEDIATE` mode assert record ID appears in queue after `enqueue`; in `DEFERRED` mode assert it does not
    - **Validates: Requirements 12.2, 12.3**

  - [ ]* 5.3 Write property test for trigger and mode-switch drain all pending records
    - **Property 13: Trigger and mode-switch drain all pending records**
    - Generate a list of pending record IDs; assert `trigger_pending` enqueues all of them; separately assert that switching `DEFERRED → IMMEDIATE` also enqueues all without a separate trigger call
    - **Validates: Requirements 12.7, 12.8**

  - [ ]* 5.4 Write property test for concurrency limit enforcement per endpoint
    - **Property 14: Concurrency limit enforcement per endpoint**
    - For a given concurrency limit C and N > C records, mock the Ollama endpoint with a counter; assert the peak simultaneous in-flight count never exceeds C
    - **Validates: Requirements 13.3, 13.4**

  - [ ]* 5.5 Write property test for endpoint fault isolation
    - **Property 15: Endpoint fault isolation**
    - Configure two mock endpoints; make one raise `OllamaUnavailableError`; assert the other endpoint continues processing and the failed records get `extraction_failed` without affecting the healthy endpoint's work
    - **Validates: Requirements 13.6**

  - [x] 5.6 Implement `_call_ollama` method in `ExtractionService`
    - Read image bytes, base64-encode, build the Ollama `POST /api/chat` payload with `FlightCardExtraction.model_json_schema()` as the format, set `temperature=0`
    - Parse response via `FlightCardExtraction.model_validate_json`; raise `ExtractionParseError` on failure, `OllamaUnavailableError` on HTTP error
    - Include the full `EXTRACTION_PROMPT` constant as defined in the design
    - _Requirements: 5.1, 5.3, 5.5, 5.9_

  - [ ]* 5.7 Write property test for extraction status monotonicity
    - **Property 10: Extraction status monotonicity**
    - Generate sequences of status transitions; assert only valid transitions (`pending → processing → extracted`, `pending → processing → extraction_failed`, `extraction_failed → pending` via re-queue) are accepted, and all others raise or are rejected
    - **Validates: Requirements 6.4**

  - [ ]* 5.8 Write property test for re-queue resets to pending and dispatches per mode
    - **Property 11: Re-queue resets to pending and dispatches per mode**
    - For each extraction mode, perform a re-queue on a `extraction_failed` record; assert status becomes `pending`; in `IMMEDIATE` mode assert it is also in the queue; in `DEFERRED` mode assert it is not
    - **Validates: Requirements 7.7, 7.8, 12.2, 12.3**

- [x] 6. Implement FastAPI routers

  - [x] 6.1 Implement `routers/scan.py` — `POST /scan`
    - Validate uploaded file is JPEG or PNG (return 400 otherwise)
    - Call `image_service.save_image`; on failure return 500 without creating a record
    - Call `record_service.create`; on failure call `image_service.delete_image` and return 500
    - Call `extraction_service.enqueue(record.id)` (no-op if deferred mode)
    - Return `ScanResponse(record_id=..., message="Card received")` with status 201
    - _Requirements: 4.1, 4.2, 4.5, 4.6, 4.7, 4.8_

  - [ ]* 6.2 Write property test for atomic submission
    - **Property 2: Atomic submission — image and record created together**
    - For arbitrary binary payloads that result in 201, assert both the file at `image_path` and the DB record exist, match, and have `extraction_status = "pending"` before any worker runs
    - **Validates: Requirements 4.1, 4.2, 4.3, 6.4**

  - [ ]* 6.3 Write property test for no partial record on write failure
    - **Property 3: No partial record on write failure**
    - Mock `image_service.save_image` to raise `ImageStorageError`; for arbitrary payloads assert response is 500 AND no `FlightRecord` exists in the DB
    - **Validates: Requirements 4.5**

  - [ ]* 6.4 Write property test for non-blocking 201 response
    - **Property 4: Non-blocking 201 response**
    - Mock `ExtractionService.enqueue` to record call order; assert 201 is returned before any Ollama call is initiated for the submitted record
    - **Validates: Requirements 4.6, 5.1**

  - [x] 6.5 Implement `routers/admin.py`
    - `POST /admin/mode` — call `extraction_service.set_mode`; return `ModeResponse`
    - `POST /admin/trigger` — call `extraction_service.trigger_pending`; return `TriggerResponse`
    - `POST /admin/requeue` — call `record_service.get_by_status("extraction_failed")`, reset each to `pending`, enqueue if in immediate mode; return `RequeueResponse`
    - `POST /admin/requeue/{record_id}` — reset single record; return 404 if not found, 422 if not `extraction_failed`; return `RequeueResponse`
    - _Requirements: 7.7, 7.8, 12.4, 12.6, 12.7, 12.8_

- [x] 7. Implement the Review UI (Jinja2 templates and review router)

  - [x] 7.1 Create `templates/base.html`
    - Include `<title>` and a visible `<h1>` heading, both populated with `event_name` from the template context
    - Link to minimal CSS; ensure layout works at ≥ 320 px viewport width
    - _Requirements: 7.6, 9.6_

  - [x] 7.2 Implement `motor_designation_str(overflow)` helper in `services/record_service.py`
    - Single motor: `"AT M2560-WT"`; cluster (same stage, multiple motors): `"2×AT J450-DMS"`; multi-stage: separate stages with ` / `
    - Return `None` if `motors` is absent or empty
    - _Requirements: 7.2_

  - [ ]* 7.3 Write property test for motor designation rendering completeness
    - **Property 21: Motor designation rendering completeness**
    - Generate arbitrary non-empty motor structures; assert the returned string is non-empty and contains the `letter` + `number` of every `MotorEntry`, with `×` for clusters and `/` for stages
    - **Validates: Requirements 5.3 (motor sub-field), 7.2**

  - [x] 7.4 Create `templates/list.html`
    - Paginated table (max 25 per page per requirements) with columns: `#`, flier name, rocket name, motor designation, flight date, status badge, created timestamp
    - Status bar showing current extraction mode (with dropdown/form to switch) and per-status counts
    - "Trigger All Pending" button (calls `POST /admin/trigger`), "Re-queue All Failed" button (visible only when failed > 0, calls `POST /admin/requeue`)
    - Search input (calls server-side on submit); pagination controls (Prev / Next)
    - _Requirements: 7.1, 7.2, 7.5, 7.6, 7.8, 12.4, 12.5, 12.6_

  - [x] 7.5 Create `templates/detail.html`
    - Two-column layout: card image (`<img src="/images/...">`, responsive, max 50 vw) and extracted fields grid showing all `FlightRecordDetail` fields
    - Status badge with the four visually distinct indicators
    - "Re-queue" button shown only when `extraction_status = "extraction_failed"` (calls `POST /admin/requeue/{id}`)
    - Fallback placeholder when image cannot be loaded (onerror handler)
    - Back-to-list link
    - _Requirements: 7.3, 7.5, 7.7, 7.9_

  - [x] 7.6 Implement `routers/review.py`
    - `GET /` — query + optional `q` search, paginate (page / page_size), compute per-status counts and current mode, render `list.html`
    - Search logic: SQL `LIKE` on `flier_name`; Python-side scan of `overflow` JSON for `rocket_name` and motor designation
    - `GET /record/{record_id}` — fetch record, build `image_url`, render `detail.html`; return 404 HTML page if not found
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_

  - [ ]* 7.7 Write property test for search result containment
    - **Property 16: Search result containment**
    - For generated queries and record lists, assert every record returned by `search_records` satisfies at least one of: `flier_name` contains Q, `overflow['rocket_name']` contains Q, or `motor_designation_str(overflow)` contains Q (all case-insensitive)
    - **Validates: Requirements 7.4**

  - [ ]* 7.8 Write property test for list view renders required fields
    - **Property 17: List view renders required fields for every record**
    - For a generated list of `FlightRecord` objects rendered through the list template, assert every row contains flier name, rocket name, motor designation, flight date, and created timestamp (or an explicit `—` placeholder)
    - **Validates: Requirements 7.2**

  - [ ]* 7.9 Write property test for event name appears in every page
    - **Property 18: Event name appears in every server-rendered page**
    - For generated `event_name` strings, render both the list and detail templates and assert the string appears in both `<title>` and a visible heading element
    - **Validates: Requirements 9.6**

- [x] 8. Checkpoint — ensure all server-side tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Wire up FastAPI app factory and startup checks

  - [x] 9.1 Create `main.py` with the FastAPI app factory and `lifespan` context manager
    - Load config, run `startup_checks` (create image store dir, init DB schema, verify static assets, log endpoints), mount `/static` and `/images` `StaticFiles`, include all routers
    - Instantiate `ExtractionService`, call `start()` on entry and `stop()` on exit
    - _Requirements: 9.1, 9.4, 9.5, 11.5, 13.8_

  - [ ]* 9.2 Write property test for processing status set before Ollama call
    - **Property 5: Processing status set before Ollama call**
    - Mock `_call_ollama` to record DB state at the moment it is first called; assert the record's `extraction_status` is `"processing"` at that point
    - **Validates: Requirements 5.2, 6.4**

- [x] 10. Implement the browser-side scanning client

  - [x] 10.1 Create `templates/scan.html`
    - `<video id="preview">` and `<canvas id="overlay">` for live preview state; `<img id="capturePreview">` for confirmation state; camera-switch `<button id="switchCamera">`; Accept/Reject buttons; spinner overlay; error/confirmation toast area
    - Load `scanner.js` and OpenCV.js from `/static/js/`
    - _Requirements: 1.1, 1.2, 1.3, 1.5, 1.6, 3.1, 3.2, 3.4_

  - [x] 10.2 Implement `static/js/scanner.js` — camera access and switching
    - `enumerateCameras()`: call `enumerateDevices`, filter `videoinput`
    - `startCamera(deviceId)`: call `getUserMedia` with `{ video: { deviceId: { exact } } }` when deviceId provided; default to environment-facing camera on first call
    - `switchCamera()`: cycle through enumerated devices, call `startCamera`
    - Display static error overlays on permission denial and unsupported `getUserMedia`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_

  - [x] 10.3 Implement the OpenCV.js detection pipeline in `scanner.js`
    - `requestAnimationFrame` loop calling `captureFrame()`
    - `captureFrame()`: draw video to offscreen canvas → cvtColor GRAY → GaussianBlur(5×5) → Canny(75, 200) → `findContours(RETR_EXTERNAL)` → `approxPolyDP` per contour → select largest 4-vertex contour → area check (`≥ MIN_FILL × frame area`)
    - `stabilityCheck()`: compare corner positions with previous frame; require < `STABILITY_THRESHOLD` px for `STABILITY_FRAMES` consecutive frames
    - `focusCheck()`: Laplacian variance on ROI ≥ `FOCUS_THRESHOLD`
    - Render detected boundary polygon on `#overlay` canvas
    - Expose all tunable constants (`MIN_FILL`, `STABILITY_THRESHOLD`, `STABILITY_FRAMES`, `FOCUS_THRESHOLD`, `OUTPUT_W`, `OUTPUT_H`) as module-level variables
    - _Requirements: 2.1, 2.2, 2.3_

  - [ ]* 10.4 Write property test for perspective transform minimum output dimensions
    - **Property 20: Perspective transform meets minimum output dimensions**
    - Generate arbitrary sets of four valid quadrilateral corner points; call `perspectiveTransform` and assert the resulting image width ≥ 1000 and height ≥ 1300
    - **Validates: Requirements 2.3, 2.5** (Note: requires jsdom or headless browser environment for OpenCV.js WASM)

  - [x] 10.5 Implement `perspectiveTransform()` and auto-capture in `scanner.js`
    - Order corners (TL, TR, BR, BL), compute `OUTPUT_W = max(1000, computed_width)` / `OUTPUT_H = max(1300, computed_height)`, call `getPerspectiveTransform` + `warpPerspective`, encode to JPEG blob / data URL
    - On stable + focused detection: call `perspectiveTransform()`, play shutter sound, transition to confirmation screen
    - _Requirements: 2.3, 2.4, 2.5_

  - [x] 10.6 Implement the confirmation screen state and submission flow in `scanner.js`
    - On entering State 2: show `#capturePreview`, Accept/Reject buttons; add swipe-up `touchstart`/`touchend` listener (vertical delta > 80 px upward → accept)
    - `submitCard(jpegDataUrl)`: convert data URL → Blob, build `FormData` with field `card_image`, `POST /scan`, show spinner, disable controls
    - On 201: display record ID for ≥ 2 s, return to State 1 (live preview)
    - On 4xx/5xx: display server error, re-enable controls, offer Retry
    - On network error or 30 s timeout: display connectivity error, re-enable controls, offer Retry
    - On Reject: discard image, return to State 1
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10_

- [x] 11. Final checkpoint — end-to-end integration and all tests pass
  - Ensure all unit tests, property-based tests, and integration tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation at logical phase boundaries
- Property tests use **Hypothesis** (Python); tag each test with `# Feature: flight-card-scanner, Property N: <text>` as specified in the design
- Unit tests and property tests complement each other; both are needed for full correctness coverage
- Client-side OpenCV.js pipeline tests (task 10.4) require a headless browser environment (e.g., Playwright + WASM) and may be deferred to manual QA if that environment is unavailable
- The `static/js/` directory must be populated by running `pnpm install` before starting the server (startup checks will exit with an error if opencv.js is missing)

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1", "2.2", "2.3"] },
    { "id": 2, "tasks": ["3.1", "3.3", "3.6"] },
    { "id": 3, "tasks": ["3.2", "3.4", "3.5", "3.7", "3.8", "5.6"] },
    { "id": 4, "tasks": ["5.1", "6.1", "6.5"] },
    { "id": 5, "tasks": ["5.2", "5.3", "5.4", "5.5", "5.7", "5.8", "6.2", "6.3", "6.4"] },
    { "id": 6, "tasks": ["7.1", "7.2"] },
    { "id": 7, "tasks": ["7.3", "7.4", "7.5", "7.6"] },
    { "id": 8, "tasks": ["7.7", "7.8", "7.9", "9.1"] },
    { "id": 9, "tasks": ["9.2", "10.1", "10.2"] },
    { "id": 10, "tasks": ["10.3"] },
    { "id": 11, "tasks": ["10.4", "10.5"] },
    { "id": 12, "tasks": ["10.6"] }
  ]
}
```
