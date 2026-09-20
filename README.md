# Flight Card Scanner

A web application for digitizing handwritten rocketry flight cards. Users photograph flight cards using their phone or tablet camera, and the app automatically extracts structured data (flier name, motor designation, flight date, etc.) using an Ollama-hosted vision language model (Qwen3-VL).

## How It Works

1. A user opens the web interface on a mobile device and photographs a flight card using the built-in camera UI.
2. The image is uploaded and stored on the server.
3. The server dispatches the image to one or more Ollama endpoints running Qwen3-VL for structured data extraction.
4. Extracted data is stored in a SQLite database and viewable through a review interface with search and pagination.

The extraction can run in two modes:
- **Immediate** -- extraction begins as soon as a card is scanned.
- **Deferred** -- images are queued and extraction is triggered manually via the admin API.

## Multi-Event Architecture

A single Flight Card Scanner instance can serve multiple events simultaneously. Each event has its own database, image store, and configuration, while sharing the server infrastructure (HTTP listener, auth system, extraction endpoints).

### How It Works

- The server config specifies an `events_dir` directory.
- The application walks `events_dir` recursively looking for `config.json` files.
- Each discovered `config.json` defines an event. The relative path from `events_dir` to the directory containing the config becomes the event's URL slug.

**Example directory layout:**

```
events/
  2025/
    summer-launch/
      config.json      -> event at /events/2025/summer-launch/
  2026/
    nxrs/
      config.json      -> event at /events/2026/nxrs/
    spring-fling/
      config.json      -> event at /events/2026/spring-fling/
```

### Lazy Loading and Idle Timeout

Events are **lazily loaded** -- their database connections and extraction services are not started until the first time someone accesses the event. This means startup is fast even with many configured events.

Events that have not been accessed for a configurable period (default: 60 minutes) are automatically **closed** to free resources. The next access will reopen the event transparently.

## Deployment Options

### Docker (recommended for production)

The application ships with a multi-stage Alpine-based Dockerfile (~220 MB image).

```bash
docker build -t flight-card-scanner .
docker run -d \
  --name flight-card-scanner \
  --restart unless-stopped \
  -v /srv/flight-cards:/data \
  -p 127.0.0.1:12345:80 \
  flight-card-scanner
```

Or use Docker Compose:

```bash
docker compose up -d
```

The container listens on port 80 internally. Mount a `/data` volume containing your `config.json` and event data. All relative paths in the config are resolved relative to the config file's directory.

See **[DEPLOY.md](DEPLOY.md)** for the full deployment guide including Tailscale Funnel configuration.

### Tailscale Funnel (HTTPS for public access)

For serving the app over the internet with automatic TLS certificates, use Tailscale Funnel on the Docker host:

```bash
sudo tailscale funnel --bg localhost:12345
```

This provisions a certificate for your `*.ts.net` domain, terminates TLS on the host, and proxies plain HTTP to the container. The app is then reachable at `https://yourhost.tail1234.ts.net`.

For tailnet-only access (no public internet):

```bash
sudo tailscale serve --bg localhost:12345
```

See **[DEPLOY.md](DEPLOY.md)** for details on requirements, Docker Compose setup, and troubleshooting.

### Local Development

```bash
cd FlightCardReader
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn[standard] sqlalchemy aiosqlite httpx \
            pydantic jinja2 python-multipart pillow rapidfuzz segno \
            itsdangerous argon2-cffi
pnpm install
python -m flight_card_scanner
```

## Prerequisites

- **Python 3.12+** (3.10+ minimum for type annotations)
- **Node.js 18+** and **pnpm** (for client-side OpenCV.js and thrustcurve-db)
- **Ollama** with the `qwen3-vl` model pulled and running

### Installing Ollama

```bash
curl -fsSL https://ollama.ai/install.sh | sh
ollama pull qwen3-vl
```

Ollama listens on `http://localhost:11434` by default.

### Installing pnpm

```bash
corepack enable
corepack prepare pnpm@latest --activate
```

Or: `npm install -g pnpm`

## Installation

Clone the repository and set up the Python virtual environment:

```bash
cd FlightCardReader

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install Python dependencies
pip install fastapi uvicorn[standard] sqlalchemy aiosqlite httpx \
            pydantic jinja2 python-multipart pillow rapidfuzz segno \
            itsdangerous argon2-cffi
```

Install the client-side JavaScript dependencies (OpenCV.js and motor database):

```bash
pnpm install
```

This places `opencv.js` into `flight_card_scanner/static/js/node_modules/` where the application expects it, and installs the `thrustcurve-db` package used for motor lookups.

## Configuration

The application supports two configuration formats:

1. **Multi-event format (recommended)** -- a server-level config plus per-event configs in the events directory tree.
2. **Legacy single-event format** -- a combined config file with both server and event settings (backward compatible).

Configuration is read from a JSON file. By default the app looks for `config.json` in the current working directory. Override with the `CONFIG_PATH` environment variable.

Path values that are not absolute are resolved relative to the directory containing the config file. This allows the same config to work regardless of the process working directory (important for Docker deployments where the config lives in `/data`).

### Multi-Event Configuration

#### Server Config (config.json)

The top-level `config.json` controls server settings and points to the events directory:

```json
{
  "host": "0.0.0.0",
  "port": 8000,
  "events_dir": "./events",
  "extraction_mode": "immediate",
  "extraction_endpoints": [
    { "url": "http://localhost:11434", "concurrency": 2 }
  ],
  "ssl_certfile": null,
  "ssl_keyfile": null,
  "auth_db_path": "./auth.db",
  "lost_rockets_db_path": "./lost_rockets.db",
  "found_rockets_db_path": "./found_rockets.db",
  "found_rockets_images_path": "./found_rockets_images",
  "session_timeout_hours": 8,
  "event_idle_timeout_minutes": 60
}
```

A fully populated, ready-to-copy example with every supported key (including
multiple extraction endpoints and both Ollama and Bedrock endpoint types) is
provided in **[`example-config.json`](example-config.json)**. A matching
per-event example is in **[`example-event-config.json`](example-event-config.json)**.

#### Server Configuration Keys

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `host` | string | `"0.0.0.0"` | Address to bind the HTTP server to |
| `port` | integer | `8000` | Port to listen on |
| `events_dir` | string | `"./events"` | Base directory to scan for event config.json files. Relative paths are resolved against the config file's directory. |
| `extraction_mode` | string | `"immediate"` | `"immediate"` or `"deferred"`. Controls whether extraction runs automatically on upload. Shared across all events. |
| `extraction_endpoints` | array | localhost:11434, concurrency 1 | List of extraction endpoints. Each entry is either an Ollama endpoint (`type` `"ollama"`, with `url` and `concurrency`) or an Amazon Bedrock endpoint (`type` `"bedrock"`, with `region`, `model_id`, and `concurrency`). Shared across all events. See [Extraction Endpoints](#extraction-endpoints). |
| `ssl_certfile` | string | *(none)* | Path to the TLS certificate file (PEM). Enables HTTPS when paired with `ssl_keyfile`. Not needed when using Tailscale Funnel. |
| `ssl_keyfile` | string | *(none)* | Path to the TLS private key file (PEM). Enables HTTPS when paired with `ssl_certfile`. |
| `auth_db_path` | string | `"./auth.db"` | Path to the shared auth SQLite database (user accounts, sessions). |
| `lost_rockets_db_path` | string | `"./lost_rockets.db"` | Path to the shared Lost Rockets SQLite database. Shared across all events so entries persist across event database rotations. See [Lost & Found Rockets](#lost--found-rockets). |
| `found_rockets_db_path` | string | `"./found_rockets.db"` | Path to the shared Found Rockets SQLite database. Shared across all events. See [Lost & Found Rockets](#lost--found-rockets). |
| `found_rockets_images_path` | string | `"./found_rockets_images"` | Directory where Found Rockets photos are stored. Created automatically if missing. Unlike per-event scans, found-rocket photos are not tied to any event's image store. |
| `session_timeout_hours` | number | `8` | Session idle timeout in hours. Range: [0.25, 8]. |
| `event_idle_timeout_minutes` | integer | `60` | Minutes of inactivity before an event's database and services are closed. Minimum: 1. |

#### Extraction Endpoints

Each entry in `extraction_endpoints` targets one extraction backend. Two types
are supported:

| Type | Required keys | Optional keys | Description |
|------|---------------|---------------|-------------|
| `ollama` (default) | `url` | `concurrency` (default `1`) | An Ollama server running a vision model (e.g. `qwen3-vl`). |
| `bedrock` | `region`, `model_id` | `concurrency` (default `1`) | An Amazon Bedrock model invoked in the given AWS region. |

`concurrency` controls how many images are dispatched to that endpoint in
parallel; the total worker count is the sum of all endpoints' `concurrency`
values. When `type` is omitted it defaults to `ollama`.

#### Per-Event Config (events/\*/config.json)

Each event directory contains its own `config.json` with event-specific settings:

```json
{
  "event_name": "NXRS Spring Launch 2026",
  "event_date_range": {
    "start": "2026-04-18",
    "end": "2026-04-19"
  },
  "known_fliers_path": "./known_fliers.tsv",
  "auto_accept_threshold": 0.95,
  "read_only": false
}
```

The `event_data_path` is automatically set to the directory containing the event's `config.json`. Images are stored in `<event_data_path>/images/` and the database at `<event_data_path>/flight_cards.db`. A fully populated example is provided in **[`example-event-config.json`](example-event-config.json)**.

> **Note:** `known_fliers_path` and `audit_log_path` are optional. If you include `known_fliers_path`, it **must** point to an existing TSV file or the event will fail to load — omit the key entirely to disable flier verification.

#### Per-Event Configuration Keys

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `event_name` | string | `"Flight Card Scanner"` | Display name shown in the web UI and page titles |
| `event_date_range` | object | today-today | Inclusive start/end dates (ISO 8601) for the launch event. Used to resolve day-of-week dates written on cards. |
| `known_fliers_path` | string | *(none)* | Path to a TSV file of known fliers for post-extraction name verification via fuzzy matching. Resolved relative to the event directory. |
| `auto_accept_threshold` | number | `0.95` | Confidence threshold for auto-accepting fuzzy name matches. |
| `read_only` | boolean | `false` | When true, disables scanning and editing for this event. See [Read-Only Mode](#read-only-mode). |
| `audit_log_path` | string | `"{event_data_path}/audit.log"` | Path to the structured audit log file for this event. |

### Legacy Single-Event Configuration

For backward compatibility, the application still supports the original combined config format where server and event settings coexist in a single file. If the config contains both server fields (`host`) and event fields (`event_name`), it is treated as a legacy combined config and a deprecation warning is logged.

```json
{
  "host": "0.0.0.0",
  "port": 80,
  "event_data_path": "./myevent",
  "event_name": "My Launch Event 2026",
  "event_date_range": {
    "start": "2026-07-04",
    "end": "2026-07-06"
  },
  "extraction_mode": "immediate",
  "extraction_endpoints": [
    { "url": "http://host.docker.internal:11434", "concurrency": 2 }
  ]
}
```

All keys from both the server and per-event tables above are accepted in legacy mode.

## Read-Only Mode

Set `"read_only": true` in config.json to lock down a completed event:

- **Database** opens in read-only mode (SQLite `?mode=ro`) -- writes are physically impossible
- **All write APIs** return `403 Forbidden` with "Event is in read-only mode"
- **UI editing controls** are hidden (Save, Verify, Extract, Requeue, Remove, motor editing, scan page)
- **Extraction service** is not started; startup migrations are skipped

This is useful for archiving an event after all cards have been processed and verified, preventing accidental modifications while still allowing browsing and reports.

## Lost & Found Rockets

Rockets sometimes drift away, land far downrange, or turn up after a launch is
over. Flight Card Scanner has two companion features for reuniting rockets with
their owners. Both work **outside the construct of any single launch event** —
each has its own shared SQLite database (configured via `lost_rockets_db_path`
and `found_rockets_db_path`) so entries persist even as individual event
databases come and go. Both are reachable from the top navigation bar on every
page, and both gate uploaded photos behind admin approval.

### Lost Rockets

The Lost Rockets list collects rockets that were **launched but not recovered**.

- While reviewing a flight record, a flier uploads a **preflight photo** of the
  rocket and checks **"mark as lost."** This adds the rocket to the Lost Rockets
  list, carrying a snapshot of the flier name, colors, diameter, length, motor,
  and flight date from the record.
- The list is public at **`/lost-rockets`** and is not tied to any one event —
  it aggregates lost rockets across every event on the server.
- If the rocket is later found, clearing "lost" on the record removes it from
  the list.

### Found Rockets

The Found Rockets list collects rockets that someone **found in the field**,
whether or not the finder knows which event or flier they belong to.

- A logged-in flier opens **`/found-rockets/report`** and uploads a **photo** of
  the rocket they found.
- **Location from photo metadata:** the app reads the **GPS coordinates
  embedded in the image's EXIF metadata** and pre-fills the latitude/longitude
  for the report. The finder can adjust these values or type in their own if the
  photo has no location data.
- The finder can add a free-text **description** and mark the rocket as
  **"still in field"** or **"recovered."**
- The list is public at **`/found-rockets`** and, like Lost Rockets, spans all
  events on the server.
- **Reuniting:** the person who posted the found rocket, or any admin/data_entry
  user, can mark it **"reunited with its flier,"** which removes it from the
  public list.

### Image Approval & Privacy

Photos uploaded to **either** list are **not visible to anyone until an admin
approves them.** This protects against accidental or inappropriate uploads
appearing publicly.

- Every uploaded image is stored under an **unguessable, randomly tokenized
  filename**, so the URL cannot be guessed before approval.
- For Found Rockets, the image-serving route additionally returns **404 to
  non-admins for unapproved photos**, so even a leaked URL reveals nothing until
  approval.
- Both features share a **single image approval queue** at
  **`/admin/preflight-queue`** (admin / data_entry only). The queue lists all
  pending preflight (Lost Rockets) and Found Rockets photos together; approving
  publishes the photo, and deleting removes the photo (and, for a rejected found
  rocket, its record).

### Relevant Configuration

| Key | Default | Purpose |
|-----|---------|---------|
| `lost_rockets_db_path` | `"./lost_rockets.db"` | Shared database of lost-rocket entries. |
| `found_rockets_db_path` | `"./found_rockets.db"` | Shared database of found-rocket entries. |
| `found_rockets_images_path` | `"./found_rockets_images"` | Directory holding found-rocket photos (created automatically). |

All three default to the config file's directory, so no configuration is
required to enable Lost & Found Rockets — the defaults work out of the box.

## Authentication

The application uses session-based authentication with three roles:

- **admin** -- Full access including user management and destructive operations.
- **data_entry** -- Can scan cards and edit records.
- **public** -- Unauthenticated users. Read-only access to review and reports pages.

### Required Environment Variable

```bash
export FCS_SESSION_SECRET="your-secret-key-at-least-16-chars"
```

The app refuses to start if `FCS_SESSION_SECRET` is not set or is shorter than 16 characters. This secret signs session cookies.

### Optional: Auto-Create Admin on First Run

```bash
export FCS_ADMIN_EMAIL="admin@example.com"
export FCS_ADMIN_PASSWORD="a-strong-password"
```

If no admin user exists in the auth database and both variables are set, an admin account is created at startup.

### Login & User Management

- **`/login`** -- Login page (email + password form).
- **`/admin/users`** -- User management interface (admin only).

### Session Details

- Cookies: `HttpOnly`, `SameSite=Lax`, `Secure` (when SSL is configured).
- Idle timeout: configurable via `session_timeout_hours` (default 8h).
- Rate limiting: 5 failed login attempts per email within 15 minutes results in HTTP 429.

## Running the Application

```bash
source .venv/bin/activate
export FCS_SESSION_SECRET="your-secret-key-at-least-16-chars"
python -m flight_card_scanner
```

This reads `config.json` (or `CONFIG_PATH`) and starts uvicorn on the configured host/port with SSL if configured.

Or use a custom config path:

```bash
CONFIG_PATH=/path/to/my-config.json python -m flight_card_scanner
```

You can also use uvicorn directly (without automatic SSL):

```bash
uvicorn flight_card_scanner.main:app --host 0.0.0.0 --port 8000
```

The app will:
1. Load the server configuration and initialize shared auth database.
2. Discover events by scanning `events_dir` for `config.json` files.
3. Verify that OpenCV.js is installed.
4. Start serving requests (event databases are opened lazily on first access).
5. Periodically check for idle events and close them to free resources.

## Using the Application

### URL Structure

In multi-event mode, all per-event pages are served under `/events/{slug}/` where the slug is derived from the event's path within `events_dir`.

For example, if `events_dir` is `./events` and a config exists at `./events/2026/nxrs/config.json`, the event's pages are at `/events/2026/nxrs/`.

### Web Interface

- **`/`** -- Events list: shows all discovered events with links, names, and date ranges.
- **`/events/{slug}/`** -- Review list for a specific event: paginated table of all scanned records with search, filters (verified status, extraction status, flight day, impulse class), and measurement proximity search.
- **`/events/{slug}/scan`** -- Camera UI: opens the device camera for capturing flight cards. Detected card edges are highlighted in real-time using OpenCV.js. Tap to capture, then accept or retake.
- **`/events/{slug}/record/{id}`** -- Detail view: shows the original image alongside all extracted fields with inline editing.
- **`/events/{slug}/reports`** -- Reports page for the event: flier counts, motor breakdown by impulse class, per-day reports.
- **`/events/{slug}/queue`** -- Extraction queue status for the event with processing indicators.
- **`/events/{slug}/admin`** -- Admin controls for the event.
- **`/login`** -- Login page (shared across all events).
- **`/admin/users`** -- User management (admin only, shared across all events).

### Navigation

- An **Events** button in the navigation bar links back to the events list (`/`).
- Page titles include both the page name and event name (e.g., "Record #42 - Missile Madness 2026").
- Admins see a **Refresh Events** button that rescans the events directory for newly added events.

### API Endpoints

Per-event endpoints (prefix with `/events/{slug}`):

- **`POST /events/{slug}/api/scan`** -- Upload a card image (multipart form, field name `card_image`). Accepts JPEG or PNG. Returns `201` with `{ "record_id": N }`.
- **`PUT /events/{slug}/api/admin/record/{id}`** -- Update fields on a record (human review corrections).
- **`POST /events/{slug}/api/admin/mode`** -- Switch extraction mode. Body: `{ "mode": "immediate" }` or `{ "mode": "deferred" }`.
- **`POST /events/{slug}/api/admin/trigger`** -- Manually trigger extraction of all pending records.
- **`POST /events/{slug}/api/admin/requeue`** -- Reset all failed records to pending and re-enqueue.
- **`POST /events/{slug}/api/admin/requeue/{record_id}`** -- Reset a single failed record.

Shared endpoints (not event-scoped):

- **`POST /api/admin/refresh-events`** -- Rescan the events directory and update the events list (admin only).
- **`POST /login`** -- Authenticate and create session.
- **`GET /logout`** -- Invalidate session and redirect to login.
- **`GET /api/admin/users`** -- List all users (admin only).
- **`POST /api/admin/users`** -- Create user (admin only).
- **`PUT /api/admin/users/{user_id}`** -- Update user (admin only).

Lost & Found Rockets endpoints (not event-scoped):

- **`GET /lost-rockets`** -- Public listing of rockets marked as lost across all events.
- **`GET /found-rockets`** -- Public listing of found rockets (approved, not-yet-reunited).
- **`GET /found-rockets/report`** -- Report form for a found rocket (login required).
- **`POST /found-rockets/api/extract-gps`** -- Extract GPS coordinates from an uploaded photo's EXIF (used to autofill the report form).
- **`POST /found-rockets/api/report`** -- Submit a found rocket (photo, description, coordinates, status). Photo starts unapproved.
- **`POST /found-rockets/api/{found_id}/reunite`** -- Mark a found rocket reunited with its flier (poster or admin/data_entry). Removes it from the list.
- **`GET /admin/preflight-queue`** -- Shared image approval queue for Lost & Found Rockets photos (admin/data_entry only).
- **`POST /api/admin/preflight/{event_slug}/{record_id}/approve`** and **`/delete`** -- Approve or delete a pending Lost Rockets (preflight) photo.
- **`POST /api/admin/found-rockets/{found_id}/approve`** and **`/delete`** -- Approve or delete a pending Found Rockets photo.

## Running Multiple Ollama Endpoints

For faster extraction at busy launches, you can distribute work across multiple machines running Ollama. List each in the `extraction_endpoints` array in the server config:

```json
"extraction_endpoints": [
  { "url": "http://localhost:11434", "concurrency": 2 },
  { "url": "http://192.168.1.50:11434", "concurrency": 3 }
]
```

The concurrency value controls how many images are sent to that endpoint in parallel. Total worker count equals the sum of all concurrency values. Extraction endpoints are shared across all events.

## HTTPS with Tailscale (direct, non-Docker)

For local development or non-Docker deployments where the app handles TLS directly:

1. Install Tailscale on the server and mobile devices
2. Generate certificates: `tailscale cert <hostname>`
3. Add `ssl_certfile` and `ssl_keyfile` to config.json
4. Start the server -- it will serve HTTPS directly

For Docker deployments, use **Tailscale Funnel** instead (TLS termination happens on the host, not in the container). See [DEPLOY.md](DEPLOY.md).

### Requirements

- Tailscale installed on the server and on any mobile devices that will scan cards
- All devices logged into the same Tailnet
- MagicDNS enabled in your Tailscale admin console (enabled by default)
- HTTPS certificates enabled in Tailscale admin console: **DNS** -> **HTTPS Certificates** -> Enable

### Generating Certificates

Run on the server (the machine running Flight Card Scanner):

```bash
tailscale cert $(tailscale status --json | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))")
```

This creates two files in the current directory:
- `<hostname>.crt` -- the certificate (signed by Let's Encrypt via Tailscale)
- `<hostname>.key` -- the private key

For example, if your machine is `cheshire.neon-tegus.ts.net`:
- `cheshire.neon-tegus.ts.net.crt`
- `cheshire.neon-tegus.ts.net.key`

### Configuring the Application

Add the certificate paths to your server `config.json`:

```json
{
  "host": "0.0.0.0",
  "port": 8000,
  "events_dir": "./events",
  "ssl_certfile": "/home/user/FlightCardReader/cheshire.neon-tegus.ts.net.crt",
  "ssl_keyfile": "/home/user/FlightCardReader/cheshire.neon-tegus.ts.net.key"
}
```

Then start the server:

```bash
python -m flight_card_scanner
```

You'll see:
```
INFO:     SSL enabled -- using cert: /home/user/FlightCardReader/cheshire.neon-tegus.ts.net.crt
INFO:     Starting HTTPS server on 0.0.0.0:8000
```

The scan page will automatically generate QR codes with `https://` URLs for Tailscale addresses, making them work with iOS camera access.

### Certificate Renewal

Tailscale certificates are valid for 90 days. When a certificate expires, the server will detect it at startup and fall back to HTTP with a clear warning:

```
WARNING:  SSL disabled -- certificate expired on 2025-09-15 12:00 UTC
          Run 'tailscale cert <hostname>' to renew
```

To renew, re-run the `tailscale cert` command and restart the server.

### Troubleshooting Tailscale HTTPS

- **"SSL not configured"** -- `ssl_certfile` and/or `ssl_keyfile` are not set in config.json
- **"certificate file not found"** -- the path in config.json doesn't point to an existing file
- **"certificate expired"** -- run `tailscale cert <hostname>` to get a fresh certificate
- **Phone can't connect** -- make sure the phone has Tailscale installed, is logged into the same tailnet, and has the VPN toggle enabled
- **Camera still blocked** -- verify you're accessing via the `https://` URL (not `http://`). The QR code on the scan page should show the correct `https://` URL for Tailscale addresses.

## Running Tests

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

## Project Structure

```
FlightCardReader/
├── Dockerfile                         # Multi-stage Alpine build
├── compose.yaml                       # Docker Compose configuration
├── DEPLOY.md                          # Full deployment guide (Docker + Tailscale Funnel)
├── config.json                        # Server-level configuration
├── example-config.json                # Fully documented server config with all keys
├── example-event-config.json          # Fully documented per-event config with all keys
├── package.json                       # pnpm manifest (opencv.js, thrustcurve-db)
├── events/                            # Events directory tree
│   └── <year>/<event>/
│       └── config.json                # Per-event configuration
├── flight_card_scanner/              # Python package (FastAPI app)
│   ├── main.py                       # App factory, lifespan, startup checks
│   ├── config.py                     # Configuration loading (ServerConfig, EventConfig, legacy AppConfig)
│   ├── event_manager.py              # Event discovery, lazy loading, idle timeout management
│   ├── database.py                   # SQLAlchemy async engine/session setup (per-event DB)
│   ├── auth_database.py              # SQLAlchemy async engine/session for shared auth DB
│   ├── models.py                     # ORM models (FlightRecord)
│   ├── auth_models.py                # Auth ORM models (User, Session)
│   ├── schemas.py                    # Pydantic request/response models
│   ├── auth_schemas.py               # Pydantic schemas for auth endpoints
│   ├── exceptions.py                 # Custom exception classes
│   ├── dependencies/
│   │   └── auth.py                   # Role enum, require_role() dependency
│   ├── middleware/
│   │   └── session_middleware.py     # Cookie-based session resolution
│   ├── routers/
│   │   ├── events.py                 # Events listing and multi-event routing
│   │   ├── scan.py                   # Card scanning UI and image upload
│   │   ├── review.py                 # List view, detail view, queue page
│   │   ├── reports.py                # Event statistics and reports
│   │   ├── admin.py                  # Admin API (mode, trigger, requeue, update)
│   │   ├── auth.py                   # Login, logout, user management, image approval queue
│   │   ├── lost_rockets.py           # Lost Rockets listing page
│   │   └── found_rockets.py          # Found Rockets listing, report form, reunite, EXIF
│   ├── lost_rockets_models.py        # ORM model for lost rockets (own DB)
│   ├── lost_rockets_database.py      # Async engine/session for the lost rockets DB
│   ├── found_rockets_models.py       # ORM model for found rockets (own DB)
│   ├── found_rockets_database.py     # Async engine/session for the found rockets DB
│   ├── services/
│   │   ├── extraction_service.py     # Ollama dispatch, worker pool, date resolution
│   │   ├── motor_lookup_service.py   # In-memory motor DB from thrustcurve-db
│   │   ├── flier_match_service.py    # Fuzzy name matching against known fliers
│   │   ├── image_service.py          # Image storage utilities
│   │   ├── found_rocket_image_service.py  # Tokenized/unguessable found-rocket image storage
│   │   ├── exif_service.py           # GPS coordinate extraction from image EXIF metadata
│   │   ├── record_service.py         # Database CRUD, unit normalization
│   │   ├── auth_service.py           # User CRUD, session lifecycle, rate limiting
│   │   └── audit_service.py          # Structured JSON Lines audit logger
│   ├── static/js/                    # Client-side JS (scanner.js, opencv.js, thrustcurve-db)
│   └── templates/                    # Jinja2 HTML templates
├── tests/                            # pytest test suite
└── .venv/                            # Python virtual environment (local dev)
```

## Troubleshooting

**"Required client-side asset missing: opencv.js"**
Run `pnpm install` from the project root.

**"Ollama returned HTTP 4xx/5xx" or records stuck in `extraction_failed`**
Verify Ollama is running (`curl http://localhost:11434/api/tags`) and that `qwen3-vl` is listed.

**Camera not working in the scan UI**
The camera API requires HTTPS on mobile browsers. Use Tailscale Funnel (Docker) or configure `ssl_certfile`/`ssl_keyfile` (local dev).

**Database locked errors**
SQLite supports limited concurrency. For high-volume events, consider running a single extraction worker per endpoint or tuning the `concurrency` values.

**Event not appearing in the list**
Make sure there is a valid `config.json` file in the event's directory under `events_dir`. Use the admin "Refresh Events" button or restart the server to re-scan for new events.

**Container can't reach Ollama on the host**
Use `http://host.docker.internal:11434` in `extraction_endpoints`. On Linux, add `--add-host=host.docker.internal:host-gateway` to `docker run`.
