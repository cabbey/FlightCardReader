# Flight Card Scanner

A web application for digitizing handwritten rocketry flight cards. Users photograph flight cards using their phone or tablet camera, and the app automatically extracts structured data (flier name, motor designation, flight date, etc.) using an Ollama-hosted vision language model (Qwen3-VL).

## How It Works

1. A user opens the web interface on a mobile device and photographs a flight card using the built-in camera UI.
2. The image is uploaded and stored on the server.
3. The server dispatches the image to one or more Ollama endpoints running Qwen3-VL for structured data extraction.
4. Extracted data is stored in a SQLite database and viewable through a review interface with search and pagination.

The extraction can run in two modes:
- **Immediate** — extraction begins as soon as a card is scanned.
- **Deferred** — images are queued and extraction is triggered manually via the admin API.

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
            pydantic jinja2 python-multipart pillow rapidfuzz segno
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

## Configuration

The application reads its configuration from a JSON file. By default it looks for `config.json` in the current working directory. Override with the `CONFIG_PATH` environment variable.

Path values that are not absolute are resolved relative to the directory containing the config file. This allows the same config to work regardless of the process working directory (important for Docker deployments where the config lives in `/data`).

### Example config.json

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

### Configuration Keys

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `host` | string | `"0.0.0.0"` | Address to bind the HTTP server to |
| `port` | integer | `8000` | Port to listen on |
| `event_data_path` | string | `"./data"` | Base directory for event data. Images stored in `<path>/images/`, database at `<path>/flight_cards.db`. Relative paths resolve against the config file's directory. |
| `event_name` | string | `"Flight Card Scanner"` | Display name shown in the web UI |
| `event_date_range` | object | today-today | Inclusive start/end dates (ISO 8601) for the event. Used to resolve day-of-week dates. |
| `extraction_mode` | string | `"immediate"` | `"immediate"` or `"deferred"`. Controls whether extraction runs automatically on upload. |
| `extraction_endpoints` | array | localhost:11434, concurrency 1 | List of Ollama endpoints. Each entry has a `url` and a `concurrency` limit (number of parallel requests). |
| `ssl_certfile` | string | *(none)* | Path to TLS certificate (PEM). Enables HTTPS when paired with `ssl_keyfile`. Not needed when using Tailscale Funnel. |
| `ssl_keyfile` | string | *(none)* | Path to TLS private key (PEM). |
| `known_fliers_path` | string | *(none)* | Path to a TSV file of known fliers for post-extraction name verification via fuzzy matching. |
| `auto_accept_threshold` | float | `0.95` | Confidence threshold for automatic flier verification. |
| `auth_db_path` | string | `"./auth.db"` | Path to the auth SQLite database (user accounts, sessions). Resolved relative to config file directory. |
| `session_timeout_hours` | number | `8` | Session idle timeout in hours. Range: [0.25, 8]. |
| `audit_log_path` | string | `"{event_data_path}/audit.log"` | Path to the structured audit log file. |
| `read_only` | boolean | `false` | When `true`, locks the event into a view-only archive. See [Read-Only Mode](#read-only-mode). |

All keys are optional — defaults are applied for any missing key.

## Read-Only Mode

Set `"read_only": true` in config.json to lock down a completed event:

- **Database** opens in read-only mode (SQLite `?mode=ro`) — writes are physically impossible
- **All write APIs** return `403 Forbidden` with "Event is in read-only mode"
- **UI editing controls** are hidden (Save, Verify, Extract, Requeue, Remove, motor editing, scan page)
- **Extraction service** is not started; startup migrations are skipped

This is useful for archiving an event after all cards have been processed and verified, preventing accidental modifications while still allowing browsing and reports.

## Authentication

The application uses session-based authentication with three roles:

- **admin** — Full access including user management and destructive operations.
- **data_entry** — Can scan cards and edit records.
- **public** — Unauthenticated users. Read-only access to review and reports pages.

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

- **`/login`** — Login page (email + password form).
- **`/admin/users`** — User management interface (admin only).

### Session Details

- Cookies: `HttpOnly`, `SameSite=Lax`, `Secure` (when SSL is configured).
- Idle timeout: configurable via `session_timeout_hours` (default 8h).
- Rate limiting: 5 failed login attempts per email within 15 minutes → HTTP 429.

## Running the Application

```bash
source .venv/bin/activate
export FCS_SESSION_SECRET="your-secret-key-at-least-16-chars"
python -m flight_card_scanner
```

## Using the Application

### Web Interface

- **`/`** — Review list: paginated table of all scanned records with search, filters (verified status, extraction status, flight day, impulse class), and measurement proximity search.
- **`/scan`** — Camera UI: opens the device camera for capturing flight cards. Card edges are highlighted in real-time using OpenCV.js.
- **`/record/{id}`** — Detail view: original image alongside all extracted fields with inline editing.
- **`/reports`** — Event statistics: flier counts, motor breakdown by impulse class, per-day reports.
- **`/queue`** — Extraction queue status with processing indicators.
- **`/login`** — Login page.
- **`/admin`** — Admin dashboard (mode control, extraction triggers).
- **`/admin/users`** — User management (admin only).

### API Endpoints

- **`POST /api/scan`** — Upload a card image (multipart form, field `card_image`). Returns `201` with `{ "record_id": N }`.
- **`POST /api/admin/mode`** — Switch extraction mode. Body: `{ "mode": "immediate" }` or `{ "mode": "deferred" }`.
- **`POST /api/admin/trigger`** — Trigger extraction of all pending records.
- **`POST /api/admin/requeue`** — Reset all failed records to pending.
- **`POST /api/admin/requeue/{record_id}`** — Reset a single failed record.
- **`PUT /api/admin/record/{id}`** — Update fields on a record (human review corrections).
- **`POST /login`** — Authenticate and create session.
- **`GET /logout`** — Invalidate session and redirect to login.
- **`GET /api/admin/users`** — List all users (admin only).
- **`POST /api/admin/users`** — Create user (admin only).
- **`PUT /api/admin/users/{user_id}`** — Update user (admin only).

## Running Multiple Ollama Endpoints

For faster extraction at busy launches, distribute work across multiple machines:

```json
"extraction_endpoints": [
  { "url": "http://localhost:11434", "concurrency": 2 },
  { "url": "http://192.168.1.50:11434", "concurrency": 3 }
]
```

Total worker count equals the sum of all concurrency values.

## HTTPS with Tailscale (direct, non-Docker)

For local development or non-Docker deployments where the app handles TLS directly:

1. Install Tailscale on the server and mobile devices
2. Generate certificates: `tailscale cert <hostname>`
3. Add `ssl_certfile` and `ssl_keyfile` to config.json
4. Start the server — it will serve HTTPS directly

For Docker deployments, use **Tailscale Funnel** instead (TLS termination happens on the host, not in the container). See [DEPLOY.md](DEPLOY.md).

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
├── config.json                        # Application configuration
├── package.json                       # pnpm manifest (opencv.js, thrustcurve-db)
├── flight_card_scanner/              # Python package (FastAPI app)
│   ├── main.py                       # App factory, lifespan, startup checks
│   ├── config.py                     # Configuration loading and validation
│   ├── database.py                   # SQLAlchemy async engine/session setup (event DB)
│   ├── auth_database.py              # SQLAlchemy async engine/session for auth DB
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
│   │   ├── scan.py                   # Card scanning UI and image upload
│   │   ├── review.py                 # List view, detail view, queue page
│   │   ├── reports.py                # Event statistics and reports
│   │   ├── admin.py                  # Admin API (mode, trigger, requeue, update)
│   │   └── auth.py                   # Login, logout, user management
│   ├── services/
│   │   ├── extraction_service.py     # Ollama dispatch, worker pool, date resolution
│   │   ├── motor_lookup_service.py   # In-memory motor DB from thrustcurve-db
│   │   ├── flier_match_service.py    # Fuzzy name matching against known fliers
│   │   ├── image_service.py          # Image storage utilities
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
SQLite supports limited concurrency. Reduce `concurrency` values or use fewer endpoints.

**Container can't reach Ollama on the host**
Use `http://host.docker.internal:11434` in `extraction_endpoints`. On Linux, add `--add-host=host.docker.internal:host-gateway` to `docker run`.
