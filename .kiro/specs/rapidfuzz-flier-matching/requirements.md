# Requirements Document

## Introduction

Replace the LLM-based flier name matching in `FlierMatchService` with a local `rapidfuzz` library implementation. The new service performs fuzzy string matching against an in-memory flier roster loaded from a TSV file, using composite scoring that combines member number lookup with name similarity. This eliminates the dependency on Ollama for flier verification while preserving the existing public interface and integration points.

## Glossary

- **FlierMatchService**: The service class responsible for loading a known-fliers TSV file and matching extracted flier data against it.
- **Roster**: The in-memory representation of all rows parsed from the known-fliers TSV file.
- **FlierMatchResult**: A dataclass returned by `match_flier()` containing match status, matched row data, and confidence information.
- **Confidence_Score**: A floating-point value between 0.0 and 1.0 representing the quality of a match.
- **Name_Similarity**: A fuzzy string comparison score produced by the `rapidfuzz` library comparing an extracted flier name against roster names.
- **Member_Number**: A numeric string (possibly with trailing letter suffix) identifying a club member in the NAR or TRA column of the roster.
- **Club**: The membership organization, either "NAR" or "TRA".
- **Cert_Level**: The certification level column in the roster (informational only, not used for matching decisions).
- **Name_Only_Threshold**: The minimum Name_Similarity score required to accept a match when no member number confirms the row.
- **Member_Confirmed_Threshold**: The lower minimum Name_Similarity score required to accept a match when the member number matches the row.
- **Auto_Accept_Threshold**: The minimum Confidence_Score (default 0.95) above which a match is automatically accepted without flagging for manual review.
- **ExtractionService**: The upstream service that calls `match_flier()` during post-extraction processing.
- **AppConfig**: The application configuration dataclass that holds all runtime settings.

## Requirements

### Requirement 1: Rapidfuzz-Based Name Matching

**User Story:** As a flight card scanner operator, I want flier name matching to use local fuzzy string comparison so that matching works without an external LLM service.

#### Acceptance Criteria

1. THE FlierMatchService SHALL use the `rapidfuzz` Python library to compute Name_Similarity between extracted flier names and Roster names.
2. THE FlierMatchService SHALL compare the extracted flier name against every name in the Roster and identify the single best match by composite score.
3. WHEN a match candidate's Name_Similarity is below the applicable threshold, THE FlierMatchService SHALL treat the candidate as unmatched.
4. THE FlierMatchService SHALL return the single best-scoring match as the result (not a list of candidates).

### Requirement 2: Member Number Lookup Strategy

**User Story:** As a flight card scanner operator, I want member numbers to be used to narrow matches so that fliers with known member numbers are matched more reliably.

#### Acceptance Criteria

1. WHEN the extracted data includes a Member_Number and a Club value, THE FlierMatchService SHALL first search the indicated Club column of the Roster for that Member_Number.
2. IF the Member_Number is not found in the indicated Club column, THEN THE FlierMatchService SHALL search the other Club column for that Member_Number.
3. WHEN the extracted data includes a Member_Number but no Club value, THE FlierMatchService SHALL search both NAR and TRA columns of the Roster for that Member_Number. This is a common input pattern because many flight cards contain only a member number without indicating which organization issued the number.
4. WHEN a row is found by Member_Number, THE FlierMatchService SHALL verify the match by computing Name_Similarity and requiring the score to meet the Member_Confirmed_Threshold.
5. WHEN multiple rows share the same Member_Number, THE FlierMatchService SHALL select the row with the highest Name_Similarity score.

### Requirement 3: Tiered Threshold Scoring

**User Story:** As a flight card scanner operator, I want a lower confidence threshold when member numbers confirm a row so that known members are matched even with imprecise name transcription.

#### Acceptance Criteria

1. THE FlierMatchService SHALL use Name_Only_Threshold for matches where no member number confirmed the row.
2. THE FlierMatchService SHALL use Member_Confirmed_Threshold (lower than Name_Only_Threshold) for matches where the member number confirmed the row.
3. THE FlierMatchService SHALL NOT use Cert_Level as a determining value for accepting or rejecting a match.

### Requirement 4: Composite Scoring and Confidence

**User Story:** As a flight card scanner operator, I want a confidence score on each match result so that low-confidence matches can be flagged for manual review.

#### Acceptance Criteria

1. THE FlierMatchResult dataclass SHALL include a `confidence` field containing the Confidence_Score of the match.
2. WHEN a match is found, THE FlierMatchService SHALL set the Confidence_Score based on the composite of member number confirmation and Name_Similarity.
3. WHEN no match is found, THE FlierMatchService SHALL return a FlierMatchResult with `matched=False` and `confidence=0.0`.
4. THE Confidence_Score SHALL be a float in the range 0.0 to 1.0 inclusive.

### Requirement 5: Public Interface Preservation

**User Story:** As a developer maintaining the extraction pipeline, I want the FlierMatchService public interface to remain stable so that integration points require minimal changes.

#### Acceptance Criteria

1. THE FlierMatchService SHALL expose a `load()` method that reads and parses the TSV file into memory.
2. THE FlierMatchService SHALL expose an `enabled` property that returns True when the Roster contains at least one data row.
3. THE FlierMatchService SHALL expose a `row_count` property that returns the number of data rows in the Roster.
4. THE FlierMatchService SHALL expose a `match_flier()` async method that accepts `flier_name`, `club`, `member_number`, and `cert_level` parameters.
5. THE `match_flier()` method SHALL NOT accept an `httpx.AsyncClient` parameter.
6. THE FlierMatchService constructor SHALL NOT require a `flier_match_model` parameter.

### Requirement 6: Configuration Changes

**User Story:** As a deployment operator, I want the configuration to no longer require a model name for flier matching so that the service can be enabled with only a fliers file path.

#### Acceptance Criteria

1. WHEN `known_fliers_path` is set in configuration, THE AppConfig loader SHALL NOT require `flier_match_model` to be present.
2. THE AppConfig dataclass SHALL retain the `flier_match_model` field as optional for backward compatibility.
3. WHEN `known_fliers_path` is set and the file exists, THE AppConfig loader SHALL accept the configuration as valid regardless of whether `flier_match_model` is present.

### Requirement 7: LLM Code Removal

**User Story:** As a developer, I want all LLM-specific logic removed from FlierMatchService so that the codebase is simpler and has no unused dependencies.

#### Acceptance Criteria

1. THE FlierMatchService SHALL NOT contain any prompt-building logic.
2. THE FlierMatchService SHALL NOT contain any Ollama HTTP call logic.
3. THE FlierMatchService SHALL NOT import or depend on the `httpx` library.
4. THE FlierMatchService SHALL NOT store or reference a model name.

### Requirement 8: Caller Integration Update

**User Story:** As a developer, I want the ExtractionService to call match_flier() without passing an HTTP client so that the integration matches the new interface.

#### Acceptance Criteria

1. WHEN the ExtractionService calls `match_flier()`, THE ExtractionService SHALL NOT pass an `httpx.AsyncClient` argument.
2. THE ExtractionService SHALL pass `flier_name`, `club`, `member_number`, and `cert_level` as keyword arguments to `match_flier()`.
3. WHEN `match_flier()` returns a FlierMatchResult with a `confidence` field, THE ExtractionService SHALL store the confidence value in the record overflow data.

### Requirement 9: Auto-Accept High-Confidence Matches

**User Story:** As a flight card scanner operator, I want high-confidence matches to be automatically accepted so that only ambiguous matches require manual review.

#### Acceptance Criteria

1. THE ExtractionService SHALL define an Auto_Accept_Threshold as a configurable constant with a default value of 0.95.
2. WHEN `match_flier()` returns a FlierMatchResult with `matched=True` and `confidence` greater than the Auto_Accept_Threshold, THE ExtractionService SHALL automatically accept the match by setting `flier_verified=True` on the record and setting `flier_match_status` to "verified" in overflow.
3. WHEN `match_flier()` returns a FlierMatchResult with `matched=True` and `confidence` less than or equal to the Auto_Accept_Threshold, THE ExtractionService SHALL set `flier_verified=False` on the record and set `flier_match_status` to "review" in overflow to flag the match for manual review.
4. WHEN a match is auto-accepted (confidence greater than Auto_Accept_Threshold), THE ExtractionService SHALL apply the matched row data to the record (updating `flier_name` and membership details from the roster row).
5. WHEN a match requires review (confidence less than or equal to Auto_Accept_Threshold), THE ExtractionService SHALL store the matched row data in overflow under `flier_match_candidate` without overwriting the record's existing `flier_name` or membership fields.
6. THE Auto_Accept_Threshold SHALL be configurable via the AppConfig alongside the other matching thresholds.
