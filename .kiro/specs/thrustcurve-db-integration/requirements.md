# Requirements Document

## Introduction

Replace the existing `ThrustCurveService` (which relies on the ThrustCurve.org HTTP API and filesystem caching) with a local, in-memory motor database loaded from the `thrustcurve-db` npm package. The package provides the complete ThrustCurve motor database as a bundled JSON file. This eliminates network dependencies, cache management complexity, and the `thrustcurve_cache_path` configuration field while preserving motor lookup and enrichment functionality.

## Glossary

- **Motor_Lookup_Service**: The replacement service module that loads motor data from the bundled JSON and provides in-memory query capabilities for motor identification and enrichment.
- **thrustcurve-db**: An npm package that ships the complete ThrustCurve.org motor database as a static JSON file, installed via pnpm alongside `opencv.js`.
- **Motor_Database**: The in-memory Python dict structure holding all motor records, keyed for efficient lookup by common name, impulse class, and manufacturer.
- **AppConfig**: The dataclass in `flight_card_scanner/config.py` that holds all runtime configuration for the application.
- **Extraction_Service**: The async worker pool that processes scanned flight cards through an LLM and annotates extracted motor data with ThrustCurve information.
- **Frontend**: The static JavaScript client served from `flight_card_scanner/static/js/` that provides the browser-based scanning interface.

## Requirements

### Requirement 1: Install thrustcurve-db Package

**User Story:** As a developer, I want the `thrustcurve-db` package installed via pnpm, so that the motor database is available as a local file without network requests.

#### Acceptance Criteria

1. WHEN `pnpm install` is run in the project root, THE Motor_Lookup_Service SHALL have access to the `thrustcurve-db` JSON data at the path `flight_card_scanner/static/js/node_modules/thrustcurve-db/`.
2. THE `package.json` SHALL include `thrustcurve-db` as a dependency.

### Requirement 2: Load Motor Database at Startup

**User Story:** As a system operator, I want the motor database loaded into memory at application startup, so that motor lookups are fast and do not require network access.

#### Acceptance Criteria

1. WHEN the application starts, THE Motor_Lookup_Service SHALL read the JSON file from the `thrustcurve-db` package located at the known `node_modules` path.
2. WHEN the application starts, THE Motor_Lookup_Service SHALL parse the JSON into an in-memory dict structure suitable for efficient querying by common name, impulse class, and manufacturer.
3. IF the `thrustcurve-db` JSON file is missing or unreadable, THEN THE Motor_Lookup_Service SHALL log an error and raise a startup failure.

### Requirement 3: Motor Search by Extracted Parameters

**User Story:** As a system operator, I want the application to match extracted motor designations against the local database, so that flight cards are enriched with accurate motor metadata without external API calls.

#### Acceptance Criteria

1. WHEN a motor common name (letter + number, e.g. "H128") is provided, THE Motor_Lookup_Service SHALL return all motors from the Motor_Database whose `commonName` field matches.
2. WHEN a manufacturer is provided alongside the common name, THE Motor_Lookup_Service SHALL filter results to motors matching that manufacturer abbreviation.
3. WHEN a single motor matches the query, THE Motor_Lookup_Service SHALL return that motor's ID as the unique identification.
4. WHEN multiple motors match the query, THE Motor_Lookup_Service SHALL return the list of candidates with their metadata (commonName, manufacturerAbbrev, designation, totImpulseNs, avgThrustN, propInfo, diameter, availability).
5. WHEN no motors match the query, THE Motor_Lookup_Service SHALL return an empty result with a descriptive message.

### Requirement 4: Manufacturer Alias Resolution

**User Story:** As a system operator, I want common manufacturer nicknames and abbreviations resolved to canonical names, so that handwritten manufacturer strings still produce correct motor matches.

#### Acceptance Criteria

1. WHEN an extracted manufacturer string matches a known alias (e.g. "AT" for "AeroTech", "CTI" for "Cesaroni"), THE Motor_Lookup_Service SHALL resolve the alias to the canonical manufacturer abbreviation used in the Motor_Database.
2. WHEN an extracted manufacturer string does not match any alias or database entry, THE Motor_Lookup_Service SHALL proceed with the search using only the common name.
3. THE Motor_Lookup_Service SHALL support case-insensitive matching for manufacturer aliases and names.

### Requirement 5: Motor Enrichment for Display

**User Story:** As a reviewer, I want flight card motor entries enriched with full motor metadata from the local database, so that I can see detailed motor specifications without waiting for network requests.

#### Acceptance Criteria

1. WHEN a motor has been uniquely identified by its database ID, THE Motor_Lookup_Service SHALL provide enrichment data including: commonName, manufacturerAbbrev, designation, propInfo, totImpulseNs, avgThrustN, diameter, and impulseClass.
2. WHEN a motor ID is not found in the Motor_Database, THE Motor_Lookup_Service SHALL return None for the enrichment data.

### Requirement 6: Remove thrustcurve_cache_path Configuration

**User Story:** As a developer, I want the `thrustcurve_cache_path` configuration field removed, so that the config is cleaner and operators do not need to manage a cache directory.

#### Acceptance Criteria

1. THE AppConfig SHALL NOT contain a `thrustcurve_cache_path` field.
2. WHEN a config.json file contains a `thrustcurve_cache_path` key, THE configuration loader SHALL ignore the key without raising an error.
3. THE application startup log SHALL NOT reference a ThrustCurve cache directory.

### Requirement 7: Remove HTTP API and Filesystem Caching Logic

**User Story:** As a developer, I want all ThrustCurve.org HTTP API calls and filesystem caching removed, so that the codebase is simpler and has no external service dependency for motor data.

#### Acceptance Criteria

1. THE Motor_Lookup_Service SHALL NOT make HTTP requests to ThrustCurve.org or any external motor data API.
2. THE Motor_Lookup_Service SHALL NOT write motor data to the filesystem as a cache.
3. THE Motor_Lookup_Service SHALL NOT read cached motor data from the filesystem.
4. THE application SHALL NOT depend on the `httpx` library for motor-related operations (other usages of `httpx` in the application are unaffected).

### Requirement 8: Preserve Existing Integration Points

**User Story:** As a developer, I want the Motor_Lookup_Service to maintain the same interface consumed by the Extraction_Service and review router, so that the replacement is transparent to the rest of the application.

#### Acceptance Criteria

1. THE Motor_Lookup_Service SHALL provide a `lookup_motors` method that accepts a list of motor dicts and annotates each with `thrustcurve_id`, `thrustcurve_candidates`, or `thrustcurve_error` fields.
2. THE Motor_Lookup_Service SHALL provide an `enrich_motors_for_display` method that accepts a list of motor dicts and returns them with `thrustcurve_data` populated for any motor with a `thrustcurve_id`.
3. THE Motor_Lookup_Service SHALL provide a `startup` method compatible with the application lifespan initialization sequence.
4. THE Extraction_Service SHALL continue to call motor lookup after successful extraction, using the Motor_Lookup_Service in place of the previous ThrustCurveService.

### Requirement 9: Frontend Access to Motor Database

**User Story:** As a frontend developer, I want the `thrustcurve-db` data accessible from the client-side JavaScript, so that the browser can perform motor lookups without round-trips to the backend.

#### Acceptance Criteria

1. WHEN the frontend needs motor data, THE Frontend SHALL be able to import or load the `thrustcurve-db` module from its location in `node_modules`.
2. THE static file serving configuration SHALL make the `thrustcurve-db` package accessible to the browser at a predictable URL path.
