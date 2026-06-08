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
- **Launch Event**: The named rocket launch event for which the system is configured, defined by a Launch Event Name (string) and a Date Range (start and end date). The Launch Event Name is displayed in the title and heading of all server-rendered HTML pages.
- **Event Configuration**: The set of configuration values that define the Launch Event, including the Launch Event Name and Date Range, stored in the JSON configuration file.

---

## Requirements

### Requirement 1: Camera Access and Live Preview

**User Story:** As a launch event volunteer, I want to point my phone or laptop camera at a flight card and see a live preview, so that I can frame the card correctly before capturing it.

#### Acceptance Criteria

1. THE Client SHALL request camera access from the browser using the `getUserMedia` API when the scanning interface is opened.
2. THE Client SHALL display a continuous live video feed from the active camera on the scanning interface.
3. WHERE a device has multiple cameras, THE Client SHALL provide a control to switch between available cameras.
4. IF the browser denies camera permission, THEN THE Client SHALL display an error message explaining that camera access is required and how to grant it.
5. IF `getUserMedia` is not supported by the browser, THEN THE Client SHALL display a message stating that a modern browser with camera support is required.

---

### Requirement 2: Card Boundary Detection, Perspective Correction, and Auto-Capture

**User Story:** As a launch event volunteer, I want the app to automatically detect the flight card's edges and capture it without me pressing a button, so that I can scan cards quickly with one hand.

#### Acceptance Criteria

1. WHILE the live video feed is active, THE Detector SHALL continuously analyze video frames to locate the boundary of a rectangular card.
2. WHEN the Detector identifies a card boundary, THE Client SHALL overlay a visual highlight on the detected boundary in the live preview.
3. WHEN a card boundary is detected, the card is stable (not in motion), the card sufficiently fills the field of view, and the image is sufficiently in focus, THE Detector SHALL automatically extract the card region from the current video frame and apply a perspective transform to produce a rectified, upright Card Image without requiring any manual trigger from the user.
4. THE Detector SHALL require all four of the following conditions to be simultaneously satisfied for a short stabilization period before triggering auto-capture: (a) a card boundary is detected, (b) the card has stopped moving, (c) the card occupies a sufficient portion of the frame, and (d) the frame is sufficiently in focus to produce a clear Card Image.
5. THE Detector SHALL produce a Card Image with a minimum resolution of 1000 × 1300 pixels.
6. WHEN the Detector auto-captures a Card Image, THE Client SHALL play an audible shutter sound to confirm the capture event to the user.

---

### Requirement 3: Capture Confirmation Screen

**User Story:** As a launch event volunteer, I want to review the captured card image before it is submitted, so that I can reject blurry or misaligned captures and retake them without wasting processing time.

#### Acceptance Criteria

1. WHEN the Detector produces a Card Image via auto-capture, THE Client SHALL immediately display the Confirmation Screen showing the captured Card Image before making any submission to the Server.
2. THE Confirmation Screen SHALL provide an on-screen "Accept" button that the user can tap to approve the Card Image for submission.
3. THE Confirmation Screen SHALL allow the user to swipe up on the displayed image as an alternative gesture to accept the Card Image for submission.
4. THE Confirmation Screen SHALL provide an on-screen "Reject" / "Retake" button that the user can tap to discard the captured Card Image and return to the live camera preview.
5. WHEN the user accepts the Card Image, THE Client SHALL submit the Card Image to the Server via an HTTP POST request with `multipart/form-data` encoding.
6. THE Client SHALL display a processing indicator while the submission is in progress.
7. WHEN the Server returns a successful response, THE Client SHALL display a confirmation that the card was received, including the Flight Record identifier, and SHALL return to the live camera preview.
8. IF the Server returns an error response, THEN THE Client SHALL display the error message returned by the Server and allow the user to resubmit or discard the image.
9. IF the network request fails before receiving a response, THEN THE Client SHALL display a connectivity error message and allow the user to retry the submission.

---

### Requirement 4: Server-Side Image Receipt and Storage

**User Story:** As a system operator, I want each submitted card image to be saved to disk immediately upon receipt — before any extraction work begins — so that no image data is ever lost even if LLM processing fails.

#### Acceptance Criteria

1. WHEN the Server receives a Card Image via HTTP POST, THE Server SHALL save the image file to the Image Store with a unique, collision-resistant filename before returning any response to the Client.
2. WHEN the Server receives a Card Image via HTTP POST, THE Server SHALL create a Flight Record in the Database with Extraction Status set to `pending` before returning any response to the Client.
3. THE Server SHALL store the filesystem path of the saved image in the corresponding Flight Record in the Database.
4. THE Server SHALL serve files from the Image Store as static HTTP assets so that the Review UI can display the original Card Image.
5. IF the Server fails to write the image to the Image Store, THEN THE Server SHALL return an HTTP 500 response with a descriptive error message and SHALL NOT create a partial Flight Record.
6. WHEN the image has been saved and the Flight Record has been created, THE Server SHALL return an HTTP 201 response to the Client containing the new Flight Record's unique identifier, without waiting for LLM extraction to complete.

---

### Requirement 5: Handwriting Recognition and Field Extraction

**User Story:** As a launch event volunteer, I want the server to read the handwritten fields on the flight card and populate the structured record, so that the information is available for review without manual re-entry.

#### Acceptance Criteria

1. AFTER the Server returns an HTTP 201 response to the Client, THE Extractor SHALL asynchronously submit the saved Card Image to the LLM with a prompt requesting structured extraction of all recognizable flight card fields.
2. WHEN the Extractor begins processing a Flight Record, THE Server SHALL update the Flight Record's Extraction Status from `pending` to `processing`.
3. THE Extractor SHALL request that the LLM attempt to extract the following fields from each Flight Card, where legible:
   - **Flight Date**: the day of week or numeric date of the flight; must fall within the configured event Date Range.
   - **Flier's Name**: the name of the person flying the rocket.
   - **Flier's Membership Information**: club affiliation (one of TRA, NAR, or CAR), member number (may include a trailing letter in NAR style), and certification level (0–3 for TRA/NAR; 0–4 for CAR).
   - **Rocket Name**: the name given to the rocket.
   - **Rocket Manufacturer / Kit**: the manufacturer or kit name of the rocket.
   - **Rocket Color(s)**: the color or colors of the rocket.
   - **Rocket Measurements**: diameter, length, and weight, each accompanied by a unit field indicating imperial or metric.
   - **Motor(s)**: a nested structure organized by stage (numbered from 1), where each stage contains a list of motors. Each motor entry may include: manufacturer (e.g., AT, Aerotech, CTI, Cesaroni, Estes), an optional leading number (CTI style, e.g., "54" in "54-2560"), a letter designation (e.g., "M"), a number following the letter, and an optional trailing letter(s) or a dash/slash followed by a number.
   - **Total Impulse**: a numeric value and a unit, where the unit is either Newton Seconds ("Ns") or Foot-Pounds ("LbsFt").
   - **Recovery Plan and Additional Notes**: free-text notes including certification flight details, competition notes, tracking information, etc.
   - **Checkboxes**: three boolean fields — "Heads Up", "First Flight", and "Complex".
   - **Rack and Pad Numbers**: rack identifier (may be a number or a string such as "L", "Low", "LowPower") and pad number (expected to be a numeric value).
   - **FSO/RSO Initials**: the initials of the safety officer who approved the flight.
   - **Post-Flight Evaluation**: an outcome value (one of "good", "motor", "airframe", or "recovery") and optional free-text comments.
4. THE Extractor SHALL accept partial results — a Flight Record MAY be updated with extracted data even if some fields are absent or marked as unreadable by the LLM.
5. WHEN the LLM returns a response, THE Extractor SHALL validate that the response is a well-formed JSON object before storing it.
6. WHEN extraction succeeds, THE Server SHALL update the Flight Record's Extraction Status to `extracted` and persist all extracted field values.
7. IF the LLM response is not valid JSON, THEN THE Extractor SHALL log the raw response and update the Flight Record's Extraction Status to `extraction_failed` with all extracted fields set to null.
8. IF the LLM service is unavailable when extraction is attempted, THEN THE Extractor SHALL update the Flight Record's Extraction Status to `extraction_failed` and log the failure.
9. THE Extractor SHALL handle multiple Flight Card layout versions by relying on the LLM's ability to interpret variable layouts without layout-specific configuration.
10. WHEN the LLM extracts a Flight Date value expressed as a day-of-week name (e.g., "Saturday"), THE Extractor SHALL resolve it to the matching calendar date within the configured Event Date Range.
11. WHEN the LLM extracts a Flight Date value, THE Extractor SHALL validate that the resolved date falls within the configured Event Date Range (start date inclusive, end date inclusive).
12. IF the extracted Flight Date cannot be resolved to a date within the Event Date Range, THEN THE Extractor SHALL store the raw extracted value in the JSON overflow column, set the `flight_date` database column to null, and set the Flight Record's Extraction Status to `extraction_failed` to flag the record for manual review.

---

### Requirement 6: Flight Record Persistence

**User Story:** As a system operator, I want each flight card submission to be stored in a local database with a clear status lifecycle, so that all records are preserved across sessions, and the current state of extraction is always visible.

#### Acceptance Criteria

1. THE Server SHALL store each Flight Record in the Database using SQLAlchemy with a SQLite backend.
2. THE Database schema SHALL include the following dedicated columns: a unique integer primary key, image file path, flight date, flier name, total impulse value (numeric), total impulse unit (text), heads_up (boolean), first_flight (boolean), complex (boolean), rack (text), pad (integer), fso_rso_initials (text), post_flight_outcome (text, one of "good", "motor", "airframe", or "recovery"), post_flight_comments (text), extraction status, and a UTC timestamp of record creation.
3. THE Database schema SHALL include a JSON overflow column to store all extracted fields not mapped to a dedicated column, including: flier's membership information (club, member number, certification level), rocket name, rocket manufacturer/kit, rocket color(s), rocket measurements (diameter, length, weight with units), motor(s) (nested stage/motor structure), and recovery plan/additional notes.
4. THE Extraction Status column SHALL enforce the following lifecycle: a new Flight Record is created with status `pending`; status transitions to `processing` when extraction begins; status transitions to `extracted` on successful extraction or to `extraction_failed` on failure.
5. THE Database SHALL be stored as a single file on the local filesystem so that it is portable and does not require a database server.

---

### Requirement 7: Review Web Interface

**User Story:** As a launch event coordinator, I want a web interface to browse and inspect all scanned flight cards, so that I can verify the extracted data and view the original card images.

#### Acceptance Criteria

1. THE Server SHALL serve a Review UI rendered via Jinja2 templates at the application root URL.
2. THE Review UI SHALL display a paginated list of all Flight Records, showing at minimum the flyer name, rocket name, motor designation, launch date, and record creation timestamp per row.
3. WHEN a user selects a Flight Record from the list, THE Review UI SHALL display a detail view showing all extracted fields alongside the original Card Image.
4. THE Review UI SHALL provide a text search input that filters Flight Records by flyer name, rocket name, or motor designation.
5. THE Review UI SHALL display the Extraction Status of each Flight Record so that records with `extraction_failed`, `processing`, or `pending` status are visually distinguishable from fully `extracted` records.
6. THE Review UI SHALL be usable on both desktop and smartphone-sized screens without horizontal scrolling.
7. THE Review UI SHALL provide a per-record "Re-queue" button on the detail view of each Flight Record whose Extraction Status is `extraction_failed`; WHEN activated, THE Server SHALL reset that record's Extraction Status to `pending` and dispatch it according to the current extraction mode (immediately if in Immediate Extraction Mode, or held in the Extraction Queue if in Deferred Extraction Mode).
8. THE Review UI SHALL provide a single "Re-queue All Failed" button on the Flight Records list view; WHEN activated, THE Server SHALL reset ALL Flight Records currently in `extraction_failed` status to `pending` and dispatch them according to the current extraction mode in a single action.

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
2. THE Server SHALL read its configuration from a JSON configuration file whose path may be specified at startup; the configuration file SHALL include at minimum: the listening host, port, Image Store directory path, Database file path, Launch Event Name (a string), event Date Range (start date and end date), extraction mode (immediate or deferred), and the list of Extraction Endpoints with their URLs and concurrency limits.
3. IF a required configuration value is absent, THEN THE Server SHALL use a documented default value and SHALL log the default being applied at startup.
4. WHEN the Server starts, THE Server SHALL verify that the Image Store directory exists and is writable, and SHALL create it if absent.
5. WHEN the Server starts, THE Server SHALL verify that the Database file is accessible and SHALL initialize the schema if the Database file does not yet exist.
6. THE Server SHALL include the configured Launch Event Name in the HTML `<title>` element and in the visible page heading of every server-rendered HTML page, so that the event identity is always visible to the operator.

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

1. THE Server SHALL support two extraction operating modes: Immediate Extraction Mode and Deferred Extraction Mode.
2. WHEN the Server is in Immediate Extraction Mode and a new Flight Record is created, THE Extractor SHALL dispatch the Flight Record for LLM processing as soon as an Extraction Endpoint has available capacity.
3. WHEN the Server is in Deferred Extraction Mode and a new Flight Record is created, THE Server SHALL retain the Flight Record with Extraction Status `pending` and SHALL NOT dispatch it to an Extraction Endpoint automatically.
4. THE Server SHALL expose an operator control (via the Review UI or a dedicated API endpoint) to switch between Immediate Extraction Mode and Deferred Extraction Mode without restarting the Server.
5. THE Review UI SHALL clearly display the current extraction operating mode so that the operator can see at a glance whether auto-dispatch is active.
6. THE Server SHALL expose an operator action (via the Review UI or a dedicated API endpoint) to manually trigger extraction of all Flight Records currently in `pending` status.
7. WHEN the manual trigger action is invoked, THE Extractor SHALL dispatch all `pending` Flight Records to available Extraction Endpoints, respecting each endpoint's configured concurrency limit.
8. IF the Server is switched from Deferred Extraction Mode to Immediate Extraction Mode, THEN THE Extractor SHALL begin dispatching any existing `pending` Flight Records to available Extraction Endpoints without requiring the operator to invoke the manual trigger separately.

---

### Requirement 13: Distributed and Remote Extraction Endpoints

**User Story:** As a system operator, I want to configure one or more Ollama instances — local or remote — as Extraction Endpoints, each with its own concurrency limit, so that extraction work can be distributed across multiple machines and completed faster.

#### Acceptance Criteria

1. THE Server SHALL accept configuration of one or more Extraction Endpoints exclusively via the JSON configuration file, where each endpoint specifies a base URL (e.g., `http://host:11434`) and a maximum concurrency value (the number of simultaneous extractions allowed against that endpoint).
2. WHEN multiple Extraction Endpoints are configured, THE Extractor SHALL distribute pending extraction work across all configured endpoints, dispatching each extraction task to the endpoint that currently has available capacity.
3. THE Extractor SHALL respect each endpoint's configured concurrency limit and SHALL NOT submit more simultaneous extraction requests to an endpoint than its configured maximum allows.
4. WHILE multiple Extraction Endpoints are configured and available, THE Extractor SHALL process extractions in parallel across endpoints so that the total number of simultaneous extractions can equal the sum of all endpoint concurrency limits.
5. THE Server SHALL support configuring a local Ollama instance and one or more remote Ollama instances simultaneously, so that local and remote extraction operate concurrently.
6. IF a configured Extraction Endpoint is unreachable when an extraction is attempted, THEN THE Extractor SHALL mark that extraction attempt as failed, update the Flight Record's Extraction Status to `extraction_failed`, log the failure including the endpoint URL, and leave the remaining configured endpoints unaffected.
7. WHERE only a single Extraction Endpoint is configured, THE Server SHALL behave identically to the prior single-endpoint behavior, preserving backward compatibility.
8. THE Server SHALL log the list of configured Extraction Endpoints and their concurrency limits at startup so that the operator can confirm the configuration is correct.
