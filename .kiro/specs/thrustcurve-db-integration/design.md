# Design Document: ThrustCurve DB Integration

## Overview

Replace the HTTP-based `ThrustCurveService` with a local `MotorLookupService` that loads motor data from the `thrustcurve-db` npm package's JSON file into memory at startup. The new service maintains the same public interface (`lookup_motors`, `enrich_motors_for_display`, `startup`) so that the `ExtractionService`, review router, and other consumers are unaffected.

## Architecture

### High-Level Data Flow

```
pnpm install → thrustcurve-db JSON file on disk
                         │
                         ▼
         MotorLookupService.startup()
                         │
                         ▼
            Parse JSON → Motor_Database (in-memory dict)
                         │
                    ┌────┴────┐
                    ▼         ▼
          lookup_motors()   enrich_motors_for_display()
                    │         │
                    ▼         ▼
         ExtractionService   Review Router
```

### Key Design Decisions

1. **Single JSON load at startup** — The entire `thrustcurve-db` dataset is loaded once into memory. The dataset is ~3–5 MB parsed, well within server memory constraints.
2. **Multi-key indexing** — Motors are indexed by `commonName` (primary), `impulseClass`, and `manufacturerAbbrev` for O(1) lookups on the hot path.
3. **Synchronous lookups** — Since all data is in memory, `lookup_motors` and `enrich_motors_for_display` remain `async` for interface compatibility but perform no I/O.
4. **Alias table preserved** — The manufacturer alias mapping from the existing service is carried over unchanged.
5. **No filesystem caching** — Eliminates the `thrustcurve_cache_path` config field and all cache read/write logic.

## Components

### 1. MotorLookupService (`flight_card_scanner/services/motor_lookup_service.py`)

The replacement for `ThrustCurveService`. A single class with the same public interface.

```python
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Path to the thrustcurve-db JSON relative to the package directory
_PACKAGE_DIR = Path(__file__).resolve().parent.parent
_THRUSTCURVE_DB_PATH = (
    _PACKAGE_DIR / "static" / "js" / "node_modules" / "thrustcurve-db" / "thrustcurve-db.json"
)

# Manufacturer alias table: lowercased alias → canonical abbreviation
_MANUFACTURER_ALIASES: dict[str, str] = {
    "at": "AeroTech",
    "aerotech": "AeroTech",
    "airotech": "AeroTech",
    "aero tech": "AeroTech",
    "ct": "Cesaroni",
    "cti": "Cesaroni",
    "cesaroni": "Cesaroni",
    "cesearoni": "Cesaroni",
    "pro24": "Cesaroni",
    "pro 24": "Cesaroni",
    "pro29": "Cesaroni",
    "pro 29": "Cesaroni",
    "pro38": "Cesaroni",
    "pro 38": "Cesaroni",
    "pro54": "Cesaroni",
    "pro 54": "Cesaroni",
    "pro75": "Cesaroni",
    "pro 75": "Cesaroni",
    "pro98": "Cesaroni",
    "pro 98": "Cesaroni",
    "pro150": "Cesaroni",
    "pro 150": "Cesaroni",
    "prox": "Cesaroni",
    "pro-x": "Cesaroni",
    "pro x": "Cesaroni",
    "amw": "AMW",
    "animal": "AMW",
    "animal motor works": "AMW",
    "loki": "Loki",
    "loki research": "Loki",
    "estes": "Estes",
    "quest": "Quest",
    "q-jet": "Quest",
    "qjet": "Quest",
    "apogee": "Apogee",
    "apogee components": "Apogee",
    "klima": "Klima",
    "contrail": "Contrail",
    "gorilla": "Gorilla",
    "gorilla rocket motors": "Gorilla",
    "hypertek": "Hypertek",
    "kosdon": "KBA",
    "kba": "KBA",
    "rattworks": "RATTWorks",
    "ratt works": "RATTWorks",
    "roadrunner": "Roadrunner",
    "rouse": "Rouse-Tech",
    "rouse-tech": "Rouse-Tech",
    "sky ripper": "Sky",
    "sky": "Sky",
    "warp 9": "WARP9",
    "warp9": "WARP9",
}


class MotorLookupService:
    """In-memory motor database loaded from thrustcurve-db npm package."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or _THRUSTCURVE_DB_PATH
        # Primary storage: list of all motor dicts
        self._motors: list[dict[str, Any]] = []
        # Indexes for fast lookup
        self._by_common_name: dict[str, list[dict[str, Any]]] = {}
        self._by_motor_id: dict[str, dict[str, Any]] = {}

    async def startup(self) -> None:
        """Load and index the motor database. Raises on failure."""
        self._load_database()

    def _load_database(self) -> None:
        """Read JSON, parse, and build indexes."""
        if not self._db_path.exists():
            raise RuntimeError(
                f"thrustcurve-db JSON not found at {self._db_path}. "
                "Run 'pnpm install' in the project root."
            )
        try:
            raw = self._db_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Failed to load thrustcurve-db: {exc}"
            ) from exc

        # data is expected to be a list of motor objects
        if isinstance(data, list):
            self._motors = data
        elif isinstance(data, dict):
            # Some versions export as {"motors": [...]}
            self._motors = data.get("motors", data.get("data", []))
        else:
            raise RuntimeError("Unexpected thrustcurve-db format")

        self._build_indexes()
        logger.info(
            "Motor database loaded: %d motors indexed", len(self._motors)
        )

    def _build_indexes(self) -> None:
        """Build lookup indexes from the motor list."""
        for motor in self._motors:
            # Index by commonName (case-insensitive key)
            cn = motor.get("commonName", "")
            if cn:
                key = cn.strip().upper()
                self._by_common_name.setdefault(key, []).append(motor)

            # Index by motorId
            mid = motor.get("motorId")
            if mid:
                self._by_motor_id[str(mid)] = motor

    def resolve_manufacturer(self, extracted_mfr: str | None) -> str | None:
        """Resolve an extracted manufacturer string to canonical abbreviation."""
        if not extracted_mfr:
            return None

        mfr_lower = extracted_mfr.strip().lower()

        # Check alias table
        if mfr_lower in _MANUFACTURER_ALIASES:
            return _MANUFACTURER_ALIASES[mfr_lower]

        # Direct match against database manufacturers
        seen: set[str] = set()
        for motor in self._motors:
            abbrev = motor.get("manufacturerAbbrev", "")
            if abbrev and abbrev not in seen:
                seen.add(abbrev)
                if mfr_lower == abbrev.lower():
                    return abbrev

        # Substring match
        for abbrev in seen:
            if mfr_lower in abbrev.lower() or abbrev.lower() in mfr_lower:
                return abbrev

        return None

    def search_motors(
        self, common_name: str, manufacturer: str | None = None
    ) -> list[dict[str, Any]]:
        """Search for motors by common name and optional manufacturer."""
        key = common_name.strip().upper()
        candidates = self._by_common_name.get(key, [])

        if manufacturer and candidates:
            resolved_mfr = self.resolve_manufacturer(manufacturer)
            if resolved_mfr:
                candidates = [
                    m for m in candidates
                    if m.get("manufacturerAbbrev", "").lower() == resolved_mfr.lower()
                ]

        return candidates

    def get_motor_by_id(self, motor_id: str) -> dict[str, Any] | None:
        """Get a single motor by its ID."""
        return self._by_motor_id.get(str(motor_id))

    async def lookup_motors(
        self, motors: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Look up each motor and annotate with results.

        For each motor dict, adds one of:
          - thrustcurve_id: str (unique match)
          - thrustcurve_candidates: list (multiple matches)
          - thrustcurve_error: str (no match or insufficient data)
        """
        for motor in motors:
            if motor.get("thrustcurve_id"):
                continue

            letter = motor.get("letter", "")
            number = motor.get("number", "")

            if not letter:
                motor["thrustcurve_error"] = "Insufficient motor data to search"
                motor.pop("thrustcurve_id", None)
                motor.pop("thrustcurve_candidates", None)
                continue

            common_name = f"{letter}{number}" if number else letter
            manufacturer = motor.get("manufacturer")
            results = self.search_motors(common_name, manufacturer)

            if len(results) == 1:
                motor["thrustcurve_id"] = str(results[0].get("motorId"))
                motor.pop("thrustcurve_candidates", None)
                motor.pop("thrustcurve_error", None)
                motor.pop("thrustcurve_data", None)
            elif len(results) > 1:
                motor["thrustcurve_candidates"] = [
                    {
                        "motorId": str(r.get("motorId")),
                        "commonName": r.get("commonName"),
                        "manufacturer": r.get("manufacturerAbbrev"),
                        "designation": r.get("designation"),
                        "totImpulseNs": r.get("totImpulseNs"),
                        "avgThrustN": r.get("avgThrustN"),
                        "propInfo": r.get("propInfo"),
                        "diameter": r.get("diameter"),
                        "availability": r.get("availability"),
                    }
                    for r in results
                ]
                motor.pop("thrustcurve_id", None)
                motor.pop("thrustcurve_error", None)
            else:
                motor["thrustcurve_error"] = f"No matches found for '{common_name}'"
                motor.pop("thrustcurve_id", None)
                motor.pop("thrustcurve_candidates", None)

        return motors

    async def enrich_motors_for_display(
        self, motors: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Enrich motor dicts with full metadata for display.

        For each motor with a thrustcurve_id, adds thrustcurve_data dict.
        """
        enriched = []
        for motor in motors:
            motor_copy = dict(motor)
            tc_id = motor_copy.get("thrustcurve_id")
            if tc_id:
                motor_data = self.get_motor_by_id(tc_id)
                if motor_data:
                    motor_copy["thrustcurve_data"] = {
                        "commonName": motor_data.get("commonName"),
                        "manufacturerAbbrev": motor_data.get("manufacturerAbbrev"),
                        "designation": motor_data.get("designation"),
                        "propInfo": motor_data.get("propInfo"),
                        "totImpulseNs": motor_data.get("totImpulseNs"),
                        "avgThrustN": motor_data.get("avgThrustN"),
                        "diameter": motor_data.get("diameter"),
                        "impulseClass": motor_data.get("impulseClass"),
                    }
                else:
                    motor_copy["thrustcurve_data"] = None
            enriched.append(motor_copy)
        return enriched
```

### 2. Configuration Changes (`flight_card_scanner/config.py`)

Remove the `thrustcurve_cache_path` field from `AppConfig` and update `load_config` to silently ignore the key if present in the JSON file.

```python
@dataclass
class AppConfig:
    """Top-level application configuration."""
    host: str = "0.0.0.0"
    port: int = 8000
    event_data_path: Path = field(default_factory=lambda: Path("./data"))
    # thrustcurve_cache_path: REMOVED
    event_name: str = "Flight Card Scanner"
    # ... rest unchanged
```

In `load_config`, the `thrustcurve_cache_path` key is simply not read from the JSON. Since the loader only picks explicit keys, any extra keys (including `thrustcurve_cache_path`) are naturally ignored.

### 3. Application Lifespan Changes (`flight_card_scanner/main.py`)

Replace `ThrustCurveService` instantiation with `MotorLookupService`:

```python
from .services.motor_lookup_service import MotorLookupService

# In lifespan():
motor_lookup_service = MotorLookupService()
await motor_lookup_service.startup()

# Pass to ExtractionService (same parameter name for compatibility)
extraction_service = ExtractionService(
    config=config,
    session_factory=session_factory,
    thrustcurve_service=motor_lookup_service,
    flier_match_service=flier_match_service,
)

# Pass to review router
review.configure(
    templates=templates, config=config,
    extraction_service=extraction_service,
    thrustcurve_service=motor_lookup_service,
)
```

Remove:
- `_log_config_summary` reference to `thrustcurve_cache_path`
- Import of `ThrustCurveService`

### 4. Package Installation (`package.json`)

Add `thrustcurve-db` as a dependency:

```json
{
  "dependencies": {
    "opencv.js": "^1.2.1",
    "thrustcurve-db": "^2.0.0"
  }
}
```

### 5. Frontend Access

The `thrustcurve-db` JSON is already accessible from the frontend because:
- It lives under `flight_card_scanner/static/js/node_modules/thrustcurve-db/`
- The existing static file mount (`/static`) serves everything under `flight_card_scanner/static/`
- Browser can load it via: `/static/js/node_modules/thrustcurve-db/thrustcurve-db.json`

No additional static file mount is needed.

## Interfaces

### MotorLookupService Public API

| Method | Signature | Description |
|--------|-----------|-------------|
| `startup` | `async def startup() -> None` | Load JSON, build indexes. Raises `RuntimeError` on failure. |
| `lookup_motors` | `async def lookup_motors(motors: list[dict]) -> list[dict]` | Annotate motors with thrustcurve_id, candidates, or error. |
| `enrich_motors_for_display` | `async def enrich_motors_for_display(motors: list[dict]) -> list[dict]` | Add thrustcurve_data for identified motors. |
| `search_motors` | `def search_motors(common_name: str, manufacturer: str | None) -> list[dict]` | Low-level search returning matching motor records. |
| `resolve_manufacturer` | `def resolve_manufacturer(extracted_mfr: str | None) -> str | None` | Alias resolution to canonical abbreviation. |
| `get_motor_by_id` | `def get_motor_by_id(motor_id: str) -> dict | None` | Direct ID lookup. |

### Motor Record Schema (from thrustcurve-db)

Key fields used by the service:

```python
{
    "motorId": "string",
    "commonName": "string",        # e.g. "H128"
    "manufacturerAbbrev": "string", # e.g. "AeroTech"
    "designation": "string",        # Full designation
    "totImpulseNs": float,
    "avgThrustN": float,
    "diameter": float,              # mm
    "impulseClass": "string",       # e.g. "H"
    "propInfo": "string",           # Propellant info
    "availability": "string",       # e.g. "regular"
}
```

## Data Model

### In-Memory Motor Database Structure

```
MotorLookupService._motors: list[dict]
    └── All motor records from JSON

MotorLookupService._by_common_name: dict[str, list[dict]]
    └── Key: uppercase commonName (e.g. "H128")
    └── Value: list of motor dicts sharing that common name

MotorLookupService._by_motor_id: dict[str, dict]
    └── Key: motorId as string
    └── Value: single motor dict
```

The indexes are built once at startup and are read-only thereafter. No locking is needed for concurrent access from async handlers.

## Error Handling

| Scenario | Behavior |
|----------|----------|
| JSON file missing at startup | `RuntimeError` raised, application fails to start |
| JSON file unreadable/corrupt | `RuntimeError` raised, application fails to start |
| Search with empty/missing letter | Returns `thrustcurve_error: "Insufficient motor data"` |
| Search with no matches | Returns `thrustcurve_error: "No matches found for 'X'"` |
| Enrichment with unknown ID | Sets `thrustcurve_data: None` in result |
| Unknown manufacturer alias | Falls through to common-name-only search |
| Config JSON has old `thrustcurve_cache_path` key | Silently ignored |

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Index Round-Trip Integrity

For any motor record present in the raw thrustcurve-db JSON, after startup indexing, querying `_by_common_name` with that motor's `commonName` (uppercased) SHALL return a list that contains that motor record.

**Validates: Requirements 2.2**

### Property 2: Search Result Correctness

For any search query with a common name and optional manufacturer, all motors in the returned result list SHALL have a `commonName` matching the query (case-insensitive), and if a manufacturer was specified and resolved, all results SHALL have a `manufacturerAbbrev` matching the resolved manufacturer.

**Validates: Requirements 3.1, 3.2**

### Property 3: Lookup Annotation Invariant

For any list of motor dicts passed to `lookup_motors`, after the call returns, each motor that did not already have a `thrustcurve_id` SHALL have exactly one of: `thrustcurve_id` (string), `thrustcurve_candidates` (non-empty list), or `thrustcurve_error` (string). No motor shall have more than one of these three fields simultaneously.

**Validates: Requirements 3.3, 3.4, 3.5, 8.1**

### Property 4: Alias Resolution Case Insensitivity

For any known alias string in the manufacturer alias table, calling `resolve_manufacturer` with any case variation of that alias SHALL return the same canonical manufacturer abbreviation.

**Validates: Requirements 4.1, 4.3**

### Property 5: Enrichment Completeness

For any motor dict with a `thrustcurve_id` that maps to a valid motor in the database, after `enrich_motors_for_display`, the resulting dict SHALL contain a `thrustcurve_data` key whose value is a dict containing all required fields: `commonName`, `manufacturerAbbrev`, `designation`, `propInfo`, `totImpulseNs`, `avgThrustN`, `diameter`, and `impulseClass`.

**Validates: Requirements 5.1, 8.2**

### Property 6: Configuration Backward Compatibility

For any valid configuration JSON that includes a `thrustcurve_cache_path` key with an arbitrary string value, calling `load_config` SHALL succeed without raising an error, and the resulting `AppConfig` SHALL not contain a `thrustcurve_cache_path` attribute.

**Validates: Requirements 6.2**
