# FlightCardReader Project Context

## Architecture Overview

This is a FastAPI web application that scans physical flight cards from model rocket launches, extracts data via LLM (Ollama/Qwen3-VL), and provides a review/verification UI.

### Key Components

- **FastAPI app** (`flight_card_scanner/main.py`) — Lifespan manages startup checks, extraction service, motor lookup, flier match service
- **Routers:**
  - `routers/review.py` — HTML pages: list (`/`), detail (`/record/{id}`), queue (`/queue`)
  - `routers/admin.py` — JSON API: `/api/admin/*` (mode, trigger, requeue, extract, update, delete, search, queue, next-unverified)
  - `routers/reports.py` — Reports page (`/reports/`)
  - `routers/scan.py` — Card scanning UI and image upload
- **Services:**
  - `services/extraction_service.py` — Worker pool, asyncio.Queue, Ollama dispatch, LLM response parsing
  - `services/record_service.py` — CRUD operations on FlightRecord
  - `services/motor_lookup_service.py` — In-memory motor DB from thrustcurve-db npm package
  - `services/flier_match_service.py` — Rapidfuzz matching against known fliers TSV roster
  - `services/image_service.py` — Image storage/processing
- **Templates:** Jinja2 in `flight_card_scanner/templates/`
- **Static:** OpenCV.js + thrustcurve-db in `flight_card_scanner/static/js/`

### Authentication & Authorization

**Auth Service (`services/auth_service.py`):**
- `AuthService` class: user CRUD, session management, rate limiting
- Password hashing: Argon2id via `argon2-cffi`, timing-safe authentication (always runs verify even for non-existent users)
- Session creation: `secrets.token_urlsafe(32)`, stored in auth DB
- Session validation: idle timeout (configurable, default 8h), hard max lifetime (8h admin / 120h data_entry)
- IP binding: strict for admin (invalidate on IP change), soft for data_entry (log and continue)
- Rate limiting: in-memory, 5 attempts per 15-min sliding window, resets on success

**Auth Database (`auth_database.py`):**
- Separate SQLite DB (not event DB) — persists across event DB rotations
- Two tables: `users` (id, email, display_name, password_hash, role, active, created_at) and `sessions` (id, user_id, created_at, last_active, is_valid, client_ip)
- `AuthBase` is separate from event DB's `Base`

**Session Middleware (`middleware/session_middleware.py`):**
- ASGI middleware wrapping the app
- Decodes signed cookie (itsdangerous `URLSafeSerializer`)
- Calls `auth_service.validate_session()` with client_ip
- Attaches user (or `None`) to `request.state.user`
- Clears cookie on invalid/expired session

**Role Dependency (`dependencies/auth.py`):**
- `Role` IntEnum: PUBLIC=0, DATA_ENTRY=1, ADMIN=2
- `require_role(min_role)` dependency factory
- API heuristic: `/api/` prefix or `Accept: application/json`

**Audit Service (`services/audit_service.py`):**
- JSON Lines format to a dedicated file (Python logging module, "audit" logger)
- `log_action(actor, action, object_type, object_id, details)`
- Fire-and-forget: catches all exceptions, logs to app logger
- Actions: created, updated, deleted, extracted, requeued, login, logout, login_failed, ip_changed
- Never logs plaintext passwords

**Auth Router (`routers/auth.py`):**
- `GET /login`, `POST /login`, `GET /logout`
- `GET /admin/users` (HTML), `GET /api/admin/users`, `POST /api/admin/users`, `PUT /api/admin/users/{id}`
- Self-demotion/self-deactivation prevention
- Session invalidation on user deactivation

**Protected Endpoints:**
- `scan.py`: `POST /api/scan` → DATA_ENTRY
- `admin.py`: all mutating → DATA_ENTRY; DELETE → ADMIN
- `review.py`, `reports.py`: all GET → PUBLIC (no auth required)

**Template Conditional Rendering:**
- `can_mutate = (not read_only) and current_user and current_user.role in ("admin", "data_entry")`
- `is_admin = current_user and current_user.role == "admin"`
- Server-side exclusion via Jinja2 `{% if %}` blocks

**Default Admin Creation:**
- On startup: if no admin exists and `FCS_ADMIN_EMAIL` + `FCS_ADMIN_PASSWORD` env vars are set → create admin
- If env vars missing → log warning, continue

### Database

SQLite via SQLAlchemy async (`aiosqlite`). Single table `flight_records` with:
- Dedicated columns: `id`, `created_at`, `image_path`, `extraction_status`, `flight_date`, `flier_name`, `total_impulse_value`, `total_impulse_unit`, flags, `rack`, `pad`, `fso_rso_initials`, `evaluation_outcome`, `evaluation_comments`, `recovery_plan`, `flier_verified`, `human_verified`
- JSON overflow column for: `motors[]`, `rocket_name`, `rocket_manufacturer`, `rocket_colors[]`, `rocket_measurements{}`, `membership{}`, `notes`, `flier_match_status`, `flier_match_confidence`, etc.

### Extraction Status Lifecycle

`pending` → `processing` → `extracted` (or `extraction_failed`)

- `human_verified = True` implies `extraction_status = "extracted"` (enforced in update_fields)
- On startup: stale "processing" records are rolled back to "pending"
- On startup: pending records with meaningful data (flier_name, motors, rocket_name, impulse, evaluation) are upgraded to "extracted"
- On startup: verified records not in "extracted" state are fixed

### Extraction Service

- Uses `asyncio.Queue[int]` for record IDs pending extraction
- Tracks `_queued_ids: set[int]` and `_processing: dict[int, {endpoint, started_at}]`
- Workers pull from queue, set status to "processing", call Ollama, then apply results
- Catch-all exception handler ensures records ALWAYS move out of "processing" state
- Mode: IMMEDIATE (auto-queue on scan) or DEFERRED (manual trigger)
- Multiple endpoints with per-endpoint concurrency semaphores

### LLM Response Parsing

In `_call_ollama` → `_parse_response`:
1. Strips `<think>...</think>` blocks
2. Pre-processes measurements: splits "2in" → (2, "in"), handles compound measurements ("4 lbs 8 oz" → 4.5 lbs)
3. Validates via Pydantic `FlightCardExtraction.model_validate_json()`
4. `model_config = {"coerce_numbers_to_str": True}` handles LLM returning ints for string fields

### Motor Lookup

- Uses `thrustcurve-db` npm package v4.0.1 (1129 motors, updated 2026-05-20)
- ThrustCurve API fallback at `https://www.thrustcurve.org/api/v1/search.json` — accepts POST with `{commonName, manufacturer, ...}` — may still be useful for motors not in the local DB
- Search logic: builds `commonName = letter + number` (e.g., "K1800"), looks up in `_by_common_name` index (exact match on uppercase)
- Motor class sort order: `¼A`, `½A`, `A`, `B`, `C`, ... `P`
- Custom manufacturers not in TC: "Ex", "Sugar"

### Flier Match

- Loads TSV roster of known fliers
- Uses `rapidfuzz.fuzz.WRatio` for fuzzy name matching
- Auto-accept threshold (default 0.95) → `flier_verified = True`
- Below threshold → `flier_match_status = "review"`
- On match: stores `membership.nar_number`, `membership.tra_number`, `membership.club`, `membership.member_number`, `membership.cert_level`
- On not_found or error: clears `overflow.membership`
- Re-verification triggers on: flier_name change, club change, member_number change

### Detail Page (record review)

- Inline editing: click any value to edit, Save/Cancel/Verify buttons
- Motor search form: Class dropdown (¼A, ½A, A-O), Avg Thrust, Manufacturer (includes "Ex", "Sugar" + TC manufacturers), Suffix
- "Save as-is" button: saves motor form values without TC lookup
- "Select" button: single always-present button, shown/hidden (not dynamically created)
- Extract button: confirms if fields are filled or status != pending
- Verify button: saves changes + sets `human_verified = True`, navigates to next unverified
- Remove (Redundant) button: requires typing "redundant" to confirm deletion
- Auto-reload after saving flier_name or membership changes (for verification badge refresh)
- All fields always visible (`show_all_fields = True`) — blank fields show clickable placeholders
- Unit fields show "units" placeholder when blank (italic gray)
- NAR/TRA numbers shown as read-only when populated by roster match; falls back to editable club/member fields otherwise
- `rocket_colors` saved as list (split on `,` `/` `;` `&` `\band\b`)

### List Page

- Sort: id_desc (default), id_asc, flier_asc, flier_desc
- Filters: verified (all/verified/unverified), status, flight_day
- Page jump dropdown
- Queue status "(queued)" label on records in extraction queue
- Filter/sort prefs persisted in localStorage (`fcs_list_prefs`) with 1-hour TTL
- Saved on every page load and filter change
- "Back to list" links on detail/queue/reports pages read localStorage prefs

### Reports Page

- Summary box: flier count, flight count, motor count, total impulse, motor breakdown pills
- Day filter dropdown (whole event or specific day)
- Sortable flyer table: name (links to search), flights, motors, impulse, breakdown
- JS sorts table on page load (name ascending)
- Impulse logic: prefer calculated from ThrustCurve data (sum of qty × totImpulseNs), fall back to card's total_impulse_value for manual motors

### Next-Unverified Navigation

- `GET /api/admin/next-unverified?after={id}`
- Filters: `human_verified == False` AND `extraction_status == "extracted"`
- Finds closest record by ID (queries both higher and lower, picks numerically closer)

### Queue Page (`/queue`)

- Shows "Processing" section (with endpoint URL, started time, live duration counter) and "Queued" section
- Auto-refreshes every 5 seconds via meta refresh
- API: `GET /api/admin/queue` → `{queued_ids: [], count: N}`

### Key Dependencies (Python)

- FastAPI + Uvicorn
- SQLAlchemy (async) + aiosqlite
- Pydantic v2
- Jinja2
- httpx (for Ollama calls)
- Pillow (image resizing)
- rapidfuzz (flier matching)
- argon2-cffi (password hashing)
- itsdangerous (cookie signing)

### Key Dependencies (JS/Static)

- thrustcurve-db v4.0.1 (npm, 1129 motors)
- opencv.js (for card scanning edge detection)

### Config

- `config.json` at project root
- Key fields: `event_name`, `event_date_range` (start/end), `extraction_endpoints[]` (url, concurrency), `extraction_mode`, `known_fliers_path`, `auto_accept_threshold`, `image_store_path`, `db_path`
- Auth config: `auth_db_path` (default `./auth.db`), `session_timeout_hours` (default 8, range [0.25, 8]), `audit_log_path` (default `{event_data_path}/audit.log`)
- Environment: `FCS_SESSION_SECRET` (required, ≥16 chars), `FCS_ADMIN_EMAIL`, `FCS_ADMIN_PASSWORD`

## Known Issues / TODOs

1. **ThrustCurve DB coverage** — v4.0.1 has 1129 motors. Some uncommon motors may still be missing. If needed, a live API fallback to `https://www.thrustcurve.org/api/v1/search.json` (POST, accepts `{commonName, manufacturer, impulseClass, ...}`) could supplement the local DB. The ThrustCurve API is free, no auth required, returns JSON matching the SearchResponse#results schema.

2. **Queue status on list page** — Records process so quickly in immediate mode that the "(queued)" label rarely shows. The dedicated `/queue` page with 5s refresh is the practical way to observe queue state.

## Conventions

- All PRs go to feature/fix branches, never directly to main
- When adding a constraint (e.g., "verified must be extracted"), include both enforcement AND migration in the same PR
- Motor class sort order: ¼A, ½A, A, B, C, ... P (unicode fractions before letters)
- Colors are stored as `list[str]`, split on `,` `/` `;` `&` `\band\b`
- localStorage key: `fcs_list_prefs` with `{sort, verified, status, flight_day, ts}` structure, 1-hour TTL
