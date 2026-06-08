# Requirements Document

## Introduction

The Flight Card Scanner is a web-based application that digitizes rocket launch "Flight Cards" — 4.25" × 5.5" paper cards with handwritten fields recording details about a rocket launch. A user points a smartphone or computer camera at a flight card; the browser detects the card boundary, corrects its perspective, and automatically captures the card image. The user reviews the captured image before accepting it, at which point the Client submits the image to a local Python server. The server immediately saves the image and creates a Flight Record, then asynchronously uses a vision LLM (Qwen2.5-VL via Ollama) to extract structured data from the card. The extracted data is stored in a local SQLite database, and a web interface is provided for reviewing the captured records. The system supports configurable Extraction Endpoints — one or more local or remote Ollama instances, each with an independent concurrency limit — allowing extraction work to be distributed across multiple machines simultaneously. Front-end JavaScript dependencies (including OpenCV.js) are managed via a package manager for easy updates. The entire system runs offline with no cloud dependencies.

---

## Glossary

- **Flight Card**: A 4.25" × 5.5" paper card containing handwritten fields about a single rocket launch. Multiple layout versions exist.
- **Card Image**: A perspective-corrected, cropped photograph of a Flight Card captured by the browser client.
- **Client**: The browser-side application, running on a smartphone or desktop browser, responsible for camera access, card detection, auto-capture, user confirmation, and image submission.
- **Server**: The local Python/FastAPI application responsible for image receipt and storage, OCR/LLM processing, data persistence, and serving the review UI.
- **Detector**: The browser-side OpenCV.js component responsible for detecting card boundaries and applying perspective correction.
- **Extractor**: The server-side component that submits a Card Image to the vision LLM and parses the structured JSON response.
- **LLM**: The Qwen2.5-VL vision language model running locally via Ollama, used for handwriting recognition and field extraction.
- **Flight Record**: A structured database record representing the data from one Flight Card, including all extracted fields, the path to the stored Card Image, and an extraction status.
- **Extraction Status**: The lifecycle state of LLM processing for a Flight Record: `pending`, `processing`, `extracted`, or `extraction_failed`.
- **Review UI**: The server-rendered web interface for browsing, searching, and inspecting Flight Records.
- **Database**: The local SQLite database managed via SQLAlchemy that stores all Flight Records.
- **Image Store**: The server-side filesystem directory where Card Images are persisted and served as static files.
- **Confirmation Screen**: The client-side UI presented after auto-capture that displays the captured Card Image for user review before submission.
- **Extraction Endpoint**: A configured Ollama instance (local or remote) to which the Extractor may submit Card Images for LLM processing, with an associated concurrency limit.
- **Extraction Queue**: The set of Flight Records with Extraction Status `pending` that are awaiting submission to an Extraction Endpoint.
- **Immediate Extraction Mode**: A server operating mode in which newly submitted Flight Records are dispatched to an Extraction Endpoint as soon as one has capacity.
- **Deferred Extraction Mode**: A server operating mode in which newly submitted Flight Records are saved with `pending` status and are not dispatched until extraction is manually triggered.
- **Launch Event**: The named rocket launch event for which the system is configured, defined by a Launch Event Name (string) and an Event Date Range (start and end date). The Launch Event Name is displayed in the title and heading of all server-rendered HTML pages.
- **Event Configuration**: The set of configuration values that define the Launch Event, including the Launch Event Name and Event Date Range, stored in the JSON configuration file.
- **Event Date Range**: The configured start date and end date (inclusive, ISO 8601 format) of the Launch Event, used to validate and resolve Flight Date values extracted from Flight Cards.
- **Flight Date**: The date of a rocket flight as recorded on the Flight Card. May be expressed as a day-of-week name (e.g., "Saturday") or a numeric date; must fall within the Event Date Range.
- **Flier's Membership Info**: A composite field capturing a flier's rocketry club affiliation (TRA, NAR, or CAR), member number (numeric, possibly with a trailing letter for NAR), and certification level (0–3 for TRA/NAR; 0–4 for CAR).
- **Motor Designation**: The structured identifier for a single rocket motor, consisting of an optional CTI prefix number, a letter designation (A–O), a thrust number, and an optional suffix (trailing letters or a dash/slash followed by a number).
- **Post-Flight Evaluation**: A structured result recorded after the flight, consisting of an outcome value (one of "good", "motor", "airframe", or "recovery") and optional free-text comments.
- **Overflow Column**: The JSON column in the Database that stores extracted fields not assigned a dedicated column, preserving all available data while keeping the schema practical.

---

## Requirements

### Requirement 1: Camera Access and Live Preview

**User Story:** As a launch event volunteer, I want to point my phone or laptop camera at a flight card and see a live preview, so that I can frame the card correctly before capturing it.

#### Acceptance Criteria

1. WHEN the scanning interface is opened, THE Client SHALL request camera access from the browser using the `getUserMedia` API.
2. THE Client SHALL display a continuous live video feed from the active camera on the scanning interface.
3. WHERE a device has multiple cameras, THE Client SHALL provide a control to switch between available cameras, and WHEN a camera is selected THE feed SHALL switch to that camera.
4. WHERE a device has multiple cameras, THE Client SHALL default to the environment-facing (rear) camera on initial load to optimize card framing.
5. IF the browser denies camera permission, THEN THE Client SHALL display an error message explaining that camera access is required and how to grant it.
6. IF `getUserMedia` is not supported by the browser, THEN THE Client SHALL display a message stating that a modern browser with camera support is required.

---

### Requirement 2: Card Boundary Detection, Perspective Correction, and Auto-Capture

**User Story:** As a launch event volunteer, I want the app to automatically detect the flight card's edges and capture it without me pressing a button, so that I can scan cards quickly with one hand.

#### Acceptance Criteria

1. WHILE the live video feed is active, THE Detector SHALL analyze video frames at a rate of at least 10 frames per second to locate the boundary of a rectangular card.
2. WHEN the Detector identifies a card boundary, THE Client SHALL overlay a visual highlight on the detected boundary in the live preview.
3. WHEN all four of the following conditions have been simultaneously satisfied for at least 500 milliseconds — (a) a four-corner card boundary is detected, (b) the detected boundary corner positions have shifted less than 10 pixels between consecutive frames, (c) the detected card region occupies at least 50% of the frame area, and (d) the Laplacian variance of the card region is at least 100 — THE Detector SHALL extract the card region from the current video frame, apply a perspective transform to produce a rectified upright Card Image, and trigger auto-capture without requiring any manual action from the user.
4. THE Detector SHALL produce a Card Image with a minimum resolution of 1000 × 1300 pixels.
5. WHEN the Detector auto-captures a Card Image, THE Client SHALL play an audible shutter sound to confirm the capture event to the user.

---

### Requirement 3: Capture Confirmation Screen

**User Story:** As a launch event volunteer, I want to review the captured card image before it is submitted, so that I can reject blurry or misaligned captures and retake them without wasting processing time.

#### Acceptance Criteria

1. WHEN the Detector produces a Card Image via auto-capture, THE Client SHALL immediately display the Confirmation Screen showing the captured Card Image before making any submission to the Server.
2. WHILE the Confirmation Screen is displayed, THE Client SHALL provide an on-screen "Accept" button that the user can tap to approve the Card Image for submission.
3. WHILE the Confirmation Screen is displayed, THE Client SHALL allow the user to swipe up on the displayed image as an alternative gesture to accept the Card Image for submission.
4. WHILE the Confirmation Screen is displayed, THE Client SHALL provide an on-screen "Reject" / "Retake" button that the user can tap to discard the captured Card Image and return to the live camera preview.
5. WHEN the user accepts the Card Image, THE Client SHALL submit the Card Image to the Server via an HTTP POST request with `multipart/form-data` encoding.
6. WHILE a submission is in progress, THE Client SHALL display a processing indicator and SHALL disable the Accept and Reject controls to prevent duplicate submissions.
7. WHEN the Server returns a successful response, THE Client SHALL display a confirmation message including the Flight Record identifier for at least 2 seconds, then SHALL return to the live camera preview.
8. IF the Server returns an error response, THEN THE Client SHALL display the error message returned by the Server, re-enable the Accept and Reject controls, and allow the user to resubmit or tap "Reject" to discard the image and return to the live camera preview.
9. IF the network request fails before receiving a response, THEN THE Client SHALL display a connectivity error message, re-enable the Accept and Reject controls, and allow the user to retry the submission or tap "Reject" to return to the live camera preview.
10. IF a submission has not received any response within 30 seconds, THEN THE Client SHALL treat it as a network failure per criterion 9.

---

### Requirement 4: Server-Side Image Receipt and Storage

**User Story:** As a system operator, I want each submitted card image to be saved to disk immediately upon receipt — before any extraction work begins — so that no image data is ever lost even if LLM processing fails.

#### Acceptance Criteria

1. WHEN the Server receives a Card Image via HTTP POST, THE Server SHALL save the image file to the Image Store using a UUID-based filename (e.g., `<uuid4>.<ext>`) before returning any response to the Client.
2. WHEN the Server receives a Card Image via HTTP POST, THE Server SHALL create a Flight Record in the Database with Extraction Status set to `pending` before returning any response to the Client.
3. THE Server SHALL store the filesystem path of the saved image in the corresponding Flight Record in the Database.
4. THE Server SHALL serve files from the Image Store as static HTTP assets so that the Review UI can display the original Card Image.
5. IF the Server fails to write the image to the Image Store, THEN THE Server SHALL return an HTTP 500 response with a descriptive error message and SHALL NOT create a Flight Record.
6. IF the image file is written successfully but the Database insert fails, THEN THE Server SHALL delete the written image file, return an HTTP 500 response with a descriptive error message, and leave no partial state in either the Database or the Image Store.
7. WHEN the image has been saved and the Flight Record has been created, THE Server SHALL return an HTTP 201 response to the Client containing the new Flight Record's unique identifier, without waiting for LLM extraction to complete.
8. IF the uploaded file is not a valid JPEG or PNG image, THEN THE Server SHALL return an HTTP 400 response with a descriptive error message and SHALL NOT write any file to the Image Store or create any Flight Record.

---

### Requirement 5: Handwriting Recognition and Field Extraction

**User Story:** As a launch event volunteer, I want the server to read the handwritten fields on the flight card and populate the structured record, so that the information is available for review without manual re-entry.

#### Acceptance Criteria

1. WHEN a Flight Record is created with Extraction Status `pending` and an Extraction Endpoint has available capacity, THE Extractor SHALL asynchronously submit the saved Card Image to the LLM with a prompt requesting structured extraction of all recognizable flight card fields.
2. WHEN the Extractor begins processing a Flight Record, THE Server SHALL update the Flight Record's Extraction Status from `pending` to `processing`.
3. THE Extractor SHALL request that the LLM return a JSON object attempting to extract the following fields from each Flight Card, where legible. Some fields may be represented as pre-printed words or options on the card that the flier has circled rather than written from scratch; the LLM SHALL recognise circled pre-printed text as the selected value for that field, in the same way it recognises handwritten text:
   - **Flight Date**: the day of week or numeric date of the flight; must fall within the configured Event Date Range. On some card versions the days of the week are pre-printed and the flier circles the applicable day; the LLM SHALL treat a circled day name as the flight date value.
   - **Flier's Name**: the name of the person flying the rocket.
   - **Flier's Membership Information**: club affiliation (one of TRA, NAR, or CAR), member number (may include a trailing letter in NAR style), and certification level (0–3 for TRA/NAR; 0–4 for CAR).
   - **Rocket Name**: the name given to the rocket.
   - **Rocket Manufacturer / Kit**: the manufacturer or kit name of the rocket.
   - **Rocket Color(s)**: the color or colors of the rocket.
   - **Rocket Measurements**: diameter, length, and weight, each accompanied by a unit field indicating imperial or metric.
   - **Motor(s)**: a nested structure organized by stage (numbered from 1), where each stage contains a list of motors. Each motor entry may include: manufacturer (e.g., AT, Aerotech, CTI, Cesaroni, Estes), an optional leading number (CTI style, e.g., "54" in "54-2560"), a letter designation (e.g., "M"), a number following the letter, and an optional trailing letter(s) or a dash/slash followed by a number.
   - **Total Impulse**: a numeric value and a unit, where the unit is either Newton Seconds ("Ns") or Foot-Pounds ("LbsFt").
   - **Recovery Plan and Additional Notes**: free-text notes including certification flight details, competition notes, tracking information, etc. On some card versions recovery method options (e.g., "parachute", "streamer", "tumble") are pre-printed and the flier circles the applicable option; the LLM SHALL treat circled pre-printed recovery options as part of the recovery plan value.
   - **Checkboxes**: three boolean fields — "Heads Up", "First Flight", and "Complex".
   - **Rack and Pad Numbers**: rack identifier (may be a number or a string such as "L", "Low", "LowPower") and pad number (expected to be a numeric value).
   - **FSO/RSO Initials**: the initials of the safety officer who approved the flight.
   - **Post-Flight Evaluation**: an outcome value (one of "good", "motor", "airframe", or "recovery") and optional free-text comments. On some card versions these outcome options are pre-printed and the flier circles the applicable result; the LLM SHALL treat a circled pre-printed outcome word as the evaluation_outcome value.
4. THE Extractor SHALL accept partial results — fields that the LLM marks as absent or unreadable SHALL be stored as `null` in the Flight Record; a Flight Record MAY be marked `extracted` even if some fields are `null`.
5. WHEN the LLM returns a response, THE Extractor SHALL validate that the response is a well-formed JSON object before storing it.
6. WHEN the LLM returns a valid JSON response, THE Server SHALL update the Flight Record's Extraction Status to `extracted` and persist all extracted field values.
7. IF the LLM response is not valid JSON, THEN THE Extractor SHALL log the raw response and update the Flight Record's Extraction Status to `extraction_failed` with all extracted fields set to null.
8. IF the LLM service is unavailable when extraction is attempted, THEN THE Extractor SHALL update the Flight Record's Extraction Status to `extraction_failed` and log the failure.
9. THE Extractor SHALL handle multiple Flight Card layout versions by relying on the LLM's ability to interpret variable layouts without layout-specific configuration.
10. WHEN the LLM extracts a Flight Date value that is expressed as a day-of-week name (e.g., "Saturday"), THE Extractor SHALL resolve it to the matching calendar date within the Event Date Range.
11. WHEN a Flight Date value has been extracted or resolved, THE Extractor SHALL validate that it falls within the configured Event Date Range.
12. IF the extracted Flight Date cannot be resolved to a date within the Event Date Range, THEN THE Extractor SHALL store the raw extracted value in the Overflow Column, set the `flight_date` database column to null, and set the Flight Record's Extraction Status to `extraction_failed` to flag the record for manual review.

---

### Requirement 6: Flight Record Persistence

**User Story:** As a system operator, I want each flight card submission to be stored in a local database with a clear status lifecycle, so that all records are preserved across sessions, and the current state of extraction is always visible.

#### Acceptance Criteria

1. THE Server SHALL store each Flight Record in the Database using SQLAlchemy with a SQLite backend.
2. THE Database schema SHALL include the following dedicated columns: a unique auto-incrementing integer primary key, image file path (TEXT, NOT NULL), `flight_date` (TEXT, nullable), `flier_name` (TEXT, nullable), `total_impulse_value` (REAL, nullable), `total_impulse_unit` (TEXT, nullable), `flag_heads_up` (BOOLEAN, nullable), `flag_first_flight` (BOOLEAN, nullable), `flag_complex` (BOOLEAN, nullable), `rack` (TEXT, nullable), `pad` (INTEGER, nullable), `safety_officer_initials` (TEXT, nullable), `evaluation_outcome` (TEXT, nullable, constrained to "good", "motor", "airframe", or "recovery"), `evaluation_comments` (TEXT, nullable), `extraction_status` (TEXT, NOT NULL), and `created_at` (TEXT, NOT NULL, server-assigned UTC timestamp in ISO 8601 format).
3. THE Database schema SHALL include an Overflow Column (`extra_fields`, JSON TEXT, nullable) to store all extracted fields not mapped to a dedicated column, including: Flier's Membership Info (club, member number, certification level), Rocket Name, Rocket Manufacturer/Kit, Rocket Color(s), Rocket Measurements (diameter, length, weight with units), Motor(s) (nested stage/motor structure), and Recovery Plan/Additional Notes.
4. WHEN a new Flight Record is created, THE Server SHALL set its `extraction_status` to `pending`.
5. WHEN the Extractor begins processing a Flight Record, THE Server SHALL update its `extraction_status` from `pending` to `processing`.
6. WHEN extraction completes successfully, THE Server SHALL update the Flight Record's `extraction_status` to `extracted`.
7. IF extraction fails for any reason, THE Server SHALL update the Flight Record's `extraction_status` to `extraction_failed`.
8. WHEN a Flight Record's status is reset to `pending` via the re-queue action, THE Server SHALL permit the transition from `extraction_failed` to `pending`.
9. THE Database SHALL be stored as a single file on the local filesystem so that it is portable and does not require a database server.

---

### Requirement 7: Review Web Interface

**User Story:** As a launch event coordinator, I want a web interface to browse and inspect all scanned flight cards, so that I can verify the extracted data and view the original card images.

#### Acceptance Criteria

1. THE Server SHALL serve a Review UI rendered via Jinja2 templates at the application root URL.
2. WHEN the list view is loaded, THE Review UI SHALL display a paginated list of Flight Records, with at most 25 records per page, each row showing at minimum: flier name, rocket name, the motor designation of the first motor in stage 1, flight date, extraction status indicator, and record creation timestamp.
3. WHEN a user selects a Flight Record from the list, THE Review UI SHALL display a detail view showing all extracted fields alongside the original Card Image.
4. WHEN the user types at least one character into the search input, THE Review UI SHALL filter the Flight Record list server-side to records whose flier name, rocket name, or stage-1 motor designation contain the search term (case-insensitive).
5. THE Review UI SHALL display the Extraction Status of each Flight Record using a distinct visual indicator (such as a colored badge or icon) for each of the four statuses: `pending`, `processing`, `extracted`, and `extraction_failed`, so that each status is unambiguously distinguishable.
6. THE Review UI SHALL be usable on screens with a minimum viewport width of 320 pixels without horizontal scrolling.
7. WHILE the detail view is displayed for a Flight Record whose `extraction_status` is `extraction_failed`, THE Review UI SHALL show a "Re-queue" button; WHEN the button is activated, THE Server SHALL reset that record's `extraction_status` to `pending`, add it to the Extraction Queue, and THE Review UI SHALL reflect the updated status.
8. WHEN the "Re-queue All Failed" button on the list view is activated, THE Server SHALL reset all Flight Records with `extraction_status` of `extraction_failed` to `pending`, add them all to the Extraction Queue, and THE Review UI SHALL reflect the updated count of pending records.
9. IF the original Card Image cannot be loaded in the detail view, THEN THE Review UI SHALL display a descriptive placeholder indicating the image is unavailable.

---

### Requirement 8: Local-Only Operation

**User Story:** As a launch event operator, I want the entire system to run without any internet connection, so that it works reliably at remote launch sites.

#### Acceptance Criteria

1. THE Server SHALL operate without making any outbound network requests to external services during normal operation.
2. THE LLM SHALL run entirely on the local machine via Ollama and SHALL NOT require an external API key or network access.
3. THE Database SHALL be a local SQLite file and SHALL NOT require a remote database service.
4. THE Client SHALL load all required JavaScript assets (including OpenCV.js) from the Server's static file serving, not from external CDNs.

---

### Requirement 9: Server Startup and Configuration

**User Story:** As a system operator, I want to start the server with a single command and configure basic parameters, so that setup at an event is fast and predictable.

#### Acceptance Criteria

1. THE Server SHALL be startable with a single command (e.g., `uvicorn main:app`) from the project directory.
2. THE Server SHALL read its configuration exclusively from a JSON configuration file; the path to the configuration file MAY be specified at startup, defaulting to `config.json` in the project directory. The configuration file SHALL include at minimum: `host` (string), `port` (integer), `image_store_path` (string), `database_path` (string), `event_name` (string), `event_start_date` (string, ISO 8601 YYYY-MM-DD), `event_end_date` (string, ISO 8601 YYYY-MM-DD), `extraction_mode` (string, "immediate" or "deferred"), and `extraction_endpoints` (array of objects, each with `url` (string) and `concurrency` (positive integer ≥ 1)).
3. IF a configuration key with a defined default is absent from the JSON file, THEN THE Server SHALL apply the documented default value and SHALL log a message at startup identifying which key was defaulted and what value was used.
4. WHEN the Server starts, THE Server SHALL verify that the Image Store directory exists and is writable; IF the directory does not exist, THE Server SHALL create it; IF the directory cannot be created or is not writable, THE Server SHALL log a descriptive error and exit.
5. WHEN the Server starts, THE Server SHALL verify that the Database file is accessible; IF the file does not exist, THE Server SHALL initialize it with the required schema; IF the file exists but cannot be opened or the schema cannot be initialized, THE Server SHALL log a descriptive error and exit.
6. THE Server SHALL include the configured `event_name` value in the HTML `<title>` element and in the visible page heading of every server-rendered HTML page.
7. IF the configuration file is absent or cannot be parsed as valid JSON, THEN THE Server SHALL log a descriptive error identifying the file path and the parse failure, and SHALL exit.

---

### Requirement 10: Image Round-Trip Fidelity

**User Story:** As a system operator, I want to confirm that the image stored on disk is identical to the image that was submitted, so that reprocessing or manual review always uses the original data.

#### Acceptance Criteria

1. FOR ALL Card Images submitted by the Client, the image file retrieved from the Image Store by the Server's static file serving SHALL be byte-for-byte identical to the content submitted in the HTTP POST body.
2. THE Server SHALL store images in a lossless or original format (PNG or JPEG at original quality) and SHALL NOT re-encode or resize images during storage.

---

### Requirement 11: Dependency Management for Client Assets

**User Story:** As a system operator, I want front-end JavaScript dependencies (such as OpenCV.js) to be managed by a package manager, so that I can update them to newer versions with a single command rather than manually downloading and replacing files.

#### Acceptance Criteria

1. THE Server's client-side asset dependencies SHALL be declared in a `package.json` manifest and managed using pnpm as the package manager.
2. THE Server SHALL serve managed client-side assets (including OpenCV.js) as static files from a directory populated by pnpm, not from manually downloaded copies.
3. WHEN a new version of a managed dependency is available, THE system SHALL allow an operator to update it by running a single pnpm command (e.g., `pnpm update`) without modifying any source file by hand.
4. THE project repository SHALL include a pnpm lockfile (`pnpm-lock.yaml`) so that all operators install identical dependency versions.
5. IF a required client-side asset is absent from the managed directory at server startup, THEN THE Server SHALL log a descriptive error identifying the missing asset and exit rather than serving a broken client.

---

### Requirement 12: Extraction Queue Control

**User Story:** As a system operator, I want to control whether newly submitted Flight Records are sent immediately for LLM extraction or held in a queue for batch processing later, so that I can defer expensive extraction work until a convenient time and trigger it on demand.

#### Acceptance Criteria

1. THE Server SHALL support two extraction operating modes: Immediate Extraction Mode and Deferred Extraction Mode, configured via the `extraction_mode` field in the JSON configuration file.
2. WHEN the Server is in Immediate Extraction Mode and a new Flight Record is created, THE Extractor SHALL dispatch the Flight Record for LLM processing as soon as an Extraction Endpoint has available capacity.
3. WHEN the Server is in Deferred Extraction Mode and a new Flight Record is created, THE Server SHALL retain the Flight Record with Extraction Status `pending` and SHALL NOT dispatch it to an Extraction Endpoint automatically.
4. THE Server SHALL expose an operator control via the Review UI to switch between Immediate Extraction Mode and Deferred Extraction Mode at runtime without restarting the Server.
5. THE Review UI SHALL display the current extraction operating mode clearly so that the operator can see at a glance whether auto-dispatch is active.
6. THE Server SHALL expose an operator action via the Review UI to manually trigger extraction of all Flight Records currently in `pending` status.
7. WHEN the manual trigger action is invoked, THE Extractor SHALL dispatch all `pending` Flight Records to available Extraction Endpoints, respecting each endpoint's configured concurrency limit.
8. IF the Server is switched from Deferred Extraction Mode to Immediate Extraction Mode, THEN THE Extractor SHALL begin dispatching any existing `pending` Flight Records to available Extraction Endpoints without requiring the operator to invoke the manual trigger separately.

---

### Requirement 13: Distributed and Remote Extraction Endpoints

**User Story:** As a system operator, I want to configure one or more Ollama instances — local or remote — as Extraction Endpoints, each with its own concurrency limit, so that extraction work can be distributed across multiple machines and completed faster.

#### Acceptance Criteria

1. THE Server SHALL accept configuration of one or more Extraction Endpoints exclusively via the JSON configuration file, where each endpoint specifies a `url` (e.g., `http://host:11434`) and a `concurrency` value (a positive integer ≥ 1 indicating the maximum number of simultaneous extractions allowed against that endpoint).
2. WHEN multiple Extraction Endpoints are configured, THE Extractor SHALL distribute pending extraction work across all configured endpoints, dispatching each extraction task to an endpoint that currently has available capacity.
3. THE Extractor SHALL respect each endpoint's configured concurrency limit and SHALL NOT submit more simultaneous extraction requests to an endpoint than its configured `concurrency` value allows.
4. WHILE multiple Extraction Endpoints are configured and available, THE Extractor SHALL process extractions in parallel across endpoints so that the total number of simultaneous extractions can equal the sum of all endpoint concurrency limits.
5. THE Server SHALL support configuring a local Ollama instance and one or more remote Ollama instances simultaneously, so that local and remote extraction operate concurrently.
6. IF a configured Extraction Endpoint is unreachable when an extraction is attempted, THEN THE Extractor SHALL update the Flight Record's Extraction Status to `extraction_failed`, log the failure including the endpoint URL, and leave the remaining configured endpoints unaffected.
7. WHERE only a single Extraction Endpoint is configured, THE Server SHALL behave identically to the multi-endpoint behavior, preserving consistency.
8. THE Server SHALL log the list of configured Extraction Endpoints and their concurrency limits at startup so that the operator can confirm the configuration is correct.
