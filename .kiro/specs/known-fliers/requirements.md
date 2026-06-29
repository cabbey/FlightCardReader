# Requirements Document

## Introduction

The Known Fliers feature validates extracted flier information against a pre-loaded roster of known club members. After the Qwen-VL vision model extracts flier details from a scanned flight card, a separate text-based LLM request matches the extracted name and membership data against a TSV file of known fliers. When a match is found, the record is updated with authoritative flier data and marked as verified. This reduces manual review effort and improves data accuracy for event organizers.

## Glossary

- **System**: The Flight Card Scanner application
- **FlightRecord**: The SQLAlchemy ORM model representing a single scanned flight card
- **ExtractionService**: The async worker pool that dispatches card images to Ollama for data extraction
- **Known Fliers File**: A TSV (tab-separated values) file exported from a flier management application containing registered flier information
- **Flier Match Model**: A text-only Ollama model configured to perform fuzzy matching of extracted flier data against the Known Fliers File
- **Extraction Endpoint**: An Ollama instance configured in `extraction_endpoints` used for both vision extraction and text-based flier matching
- **Flier Verification**: The process of matching extracted flier information against the Known Fliers File and updating the record with authoritative data

## Requirements

### Requirement 1: Known Fliers File Configuration

**User Story:** As an event organizer, I want to specify the path to my known fliers TSV file in the application configuration, so that the system can load flier data for matching.

#### Acceptance Criteria

1. THE System SHALL accept a `known_fliers_path` field in the configuration JSON file specifying the filesystem path to a TSV file.
2. WHEN `known_fliers_path` is absent from the configuration, THE System SHALL disable flier verification and log an INFO-level message indicating the feature is inactive.
3. WHEN `known_fliers_path` is present and the file does not exist at the specified path, THE System SHALL raise a ConfigError during configuration loading.
4. THE System SHALL accept a `flier_match_model` field in the configuration JSON file specifying the Ollama model name to use for flier matching.
5. WHEN `flier_match_model` is absent from the configuration and `known_fliers_path` is present, THE System SHALL raise a ConfigError indicating that both fields are required together.

### Requirement 2: Known Fliers File Loading

**User Story:** As an event organizer, I want the known fliers data loaded once at startup and kept in memory, so that matching is fast and does not require repeated file reads.

#### Acceptance Criteria

1. WHEN the application starts and `known_fliers_path` is configured, THE System SHALL read and parse the TSV file into memory.
2. THE System SHALL treat the first row of the TSV file as a header row containing column names.
3. THE System SHALL parse each subsequent row into a structured record containing at minimum: flier name, NAR number, TRA number, and certification level.
4. THE System SHALL retain all parsed flier rows in memory for the lifetime of the application process.
5. IF the TSV file is empty or contains only a header row, THEN THE System SHALL log a WARNING-level message and disable flier verification.

### Requirement 3: Flier Verified Database Column

**User Story:** As an event organizer, I want each flight record to indicate whether its flier information has been verified against the known fliers list, so that I can distinguish verified records from unverified ones.

#### Acceptance Criteria

1. THE System SHALL store a `flier_verified` boolean column on the FlightRecord model with a default value of false.
2. WHEN a flier match is successfully found, THE System SHALL set `flier_verified` to true on the corresponding FlightRecord.
3. WHEN no flier match is found, THE System SHALL set `flier_verified` to false on the corresponding FlightRecord.

### Requirement 4: Flier Match LLM Request

**User Story:** As an event organizer, I want the system to use an LLM to fuzzy-match extracted flier data against the known fliers list, so that minor handwriting recognition errors and name variations are handled gracefully.

#### Acceptance Criteria

1. WHEN extraction completes successfully for a FlightRecord and flier verification is enabled, THE System SHALL send a text-only chat request to an Extraction Endpoint using the Flier Match Model.
2. THE System SHALL include the full contents of the Known Fliers File in the LLM prompt.
3. THE System SHALL include the extracted flier name and membership information (club, member number, certification level) from the FlightRecord in the LLM prompt.
4. THE System SHALL instruct the LLM to return the 1-indexed line number of the best matching row from the Known Fliers File, where row 1 is the header row.
5. THE System SHALL instruct the LLM to return 0 if no sufficiently close match exists.
6. THE System SHALL parse the LLM response as an integer line number.
7. IF the LLM response cannot be parsed as an integer, THEN THE System SHALL log a WARNING-level message and set the extraction status to `flier_match_failed`.

### Requirement 5: Successful Flier Match Handling

**User Story:** As an event organizer, I want the system to update flight records with authoritative flier data when a match is found, so that minor OCR errors are corrected automatically.

#### Acceptance Criteria

1. WHEN the LLM returns a line number greater than 0, THE System SHALL load the corresponding row from the in-memory Known Fliers data.
2. WHEN a valid match row is loaded, THE System SHALL update the FlightRecord `flier_name` field with the name from the matched row.
3. WHEN a valid match row is loaded, THE System SHALL update the FlightRecord membership information (member number, certification level) with values from the matched row in the overflow JSON.
4. WHEN a valid match row is loaded, THE System SHALL set `flier_verified` to true on the FlightRecord.
5. IF the LLM returns a line number that exceeds the number of rows in the Known Fliers File, THEN THE System SHALL log a WARNING-level message and treat the result as no match found.

### Requirement 6: No Match Handling

**User Story:** As an event organizer, I want to distinguish between records that have not yet been matched and records where no match was found, so that I can prioritize manual review.

#### Acceptance Criteria

1. WHEN the LLM returns 0 (no match), THE System SHALL set the FlightRecord extraction status to `flier_not_found`.
2. THE System SHALL leave `flier_verified` as false when no match is found.
3. THE System SHALL preserve the originally extracted flier name and membership data on the FlightRecord when no match is found.

### Requirement 7: Integration with Extraction Pipeline

**User Story:** As a developer, I want flier verification to run as a post-extraction step in the existing pipeline, so that it follows the established pattern for post-processing.

#### Acceptance Criteria

1. THE System SHALL execute flier verification after the ThrustCurve motor lookup step in the extraction pipeline.
2. THE System SHALL use the same Extraction Endpoints (Ollama instances) for flier matching as for vision extraction.
3. THE System SHALL send a text-only chat payload (no images) to the Flier Match Model for matching requests.
4. IF flier verification fails due to an Ollama endpoint error, THEN THE System SHALL log the error and leave the FlightRecord extraction status as `extracted` without blocking the overall extraction result.
5. WHILE flier verification is disabled (no `known_fliers_path` configured), THE System SHALL skip the flier verification step without error.

### Requirement 8: Prompt Design for Large Flier Lists

**User Story:** As an event organizer with a large club roster (500+ members), I want the system to handle large flier lists effectively in the LLM prompt, so that matching remains accurate.

#### Acceptance Criteria

1. THE System SHALL include the Known Fliers File contents as numbered lines in the LLM prompt to enable unambiguous line-number responses.
2. THE System SHALL format the prompt to clearly delineate the flier list from the matching instructions and the extracted data.
3. THE System SHALL set appropriate context window parameters on the LLM request to accommodate prompts containing 500 or more flier rows.

### Requirement 9: Flier Match Model Recommendation

**User Story:** As an event organizer, I want guidance on which Ollama model to use for flier matching, so that I can configure the system for accurate results.

#### Acceptance Criteria

1. THE System SHALL document a recommended Ollama model for the `flier_match_model` configuration field in project documentation.
2. THE System SHALL select a model recommendation based on accuracy at text matching and fuzzy string comparison tasks, ability to follow structured output instructions, and ability to run within typical Ollama resource constraints.
