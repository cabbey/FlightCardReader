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

## Prerequisites

- **Ubuntu Linux** (22.04 or later recommended)
- **Python 3.13+**
- **Node.js 18+** and **pnpm** (for client-side OpenCV.js dependency)
- **Ollama** with the `qwen3-vl` model pulled and running

### Installing Ollama

Follow the official instructions at [https://ollama.com/download/linux](https://ollama.com/download/linux):

```bash
curl -fsSL https://ollama.ai/install.sh | sh
```

Then pull the vision model:

```bash
ollama pull qwen3-vl
```

Ollama listens on `http://localhost:11434` by default.

### Installing pnpm

If you don't already have pnpm:

```bash
npm install -g pnpm
```

Or via corepack (bundled with Node.js 16.9+):

```bash
corepack enable
corepack prepare pnpm@latest --activate
```

## Installation

Clone the repository and set up the Python virtual environment:

```bash
cd FlightCardReader

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install Python dependencies
pip install fastapi uvicorn[standard] sqlalchemy aiosqlite httpx \
            pydantic jinja2 python-multipart
```

Install the client-side JavaScript dependencies (OpenCV.js):

```bash
pnpm install
```

This places `opencv.js` into `flight_card_scanner/static/js/node_modules/` where the application expects it.

## Configuration

The application reads its configuration from a JSON file. By default it looks for `config.json` in the current working directory. You can override this with the `CONFIG_PATH` environment variable.

### config.json

```json
{
  "host": "0.0.0.0",
  "port": 8000,
  "event_data_path": "./data",
  "thrustcurve_cache_path": "./thrustcurve_cache",
  "event_name": "My Launch Event 2025",
  "event_date_range": {
    "start": "2025-07-18",
    "end": "2025-07-20"
  },
  "extraction_mode": "immediate",
  "extraction_endpoints": [
    { "url": "http://localhost:11434", "concurrency": 2 }
  ]
}
```

### Configuration Keys

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `host` | string | `"0.0.0.0"` | Address to bind the HTTP server to |
| `port` | integer | `8000` | Port to listen on |
| `event_data_path` | string | `"./data"` | Base directory for event data. Images are stored in `<event_data_path>/images/` and the database at `<event_data_path>/flight_cards.db`. |
| `thrustcurve_cache_path` | string | `"./thrustcurve_cache"` | Directory for caching ThrustCurve.org motor data. |
| `event_name` | string | `"Flight Card Scanner"` | Display name shown in the web UI |
| `event_date_range` | object | today–today | Inclusive start/end dates (ISO 8601) for the launch event. Used to resolve day-of-week dates written on cards. |
| `extraction_mode` | string | `"immediate"` | `"immediate"` or `"deferred"`. Controls whether extraction runs automatically on upload. |
| `extraction_endpoints` | array | localhost:11434, concurrency 1 | List of Ollama endpoints. Each entry has a `url` and a `concurrency` limit (number of parallel requests). |
| `ssl_certfile` | string | *(none)* | Path to the TLS certificate file (PEM). Optional — enables HTTPS when paired with `ssl_keyfile`. |
| `ssl_keyfile` | string | *(none)* | Path to the TLS private key file (PEM). Optional — enables HTTPS when paired with `ssl_certfile`. |
| `known_fliers_path` | string | *(none)* | Path to a TSV file of known fliers for post-extraction name verification. Requires `flier_match_model` to also be set. |
| `flier_match_model` | string | *(none)* | Ollama model name used for flier name matching (e.g., `qwen2.5:7b`). Required when `known_fliers_path` is set. |
| `auth_db_path` | string | `"./auth.db"` | Path to the auth SQLite database (user accounts, sessions). Resolved relative to config file directory. |
| `session_timeout_hours` | number | `8` | Session idle timeout in hours. Range: [0.25, 8]. |
| `audit_log_path` | string | `"{event_data_path}/audit.log"` | Path to the structured audit log file. |

All keys are optional — defaults are applied for any missing key.

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

Make sure Ollama is running, then start the server:

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
1. Create the image store directory if it doesn't exist.
2. Initialize the SQLite database schema.
3. Verify that OpenCV.js is installed.
4. Start extraction worker tasks for each configured endpoint.

## Using the Application

### Web Interface

- **`/`** — Review list: paginated table of all scanned records with search.
- **`/scan`** (browser) — Camera UI: opens the device camera for capturing flight cards. Detected card edges are highlighted in real-time using OpenCV.js. Tap to capture, then accept or retake.
- **`/record/{id}`** — Detail view: shows the original image alongside all extracted fields.
- **`/login`** — Login page.
- **`/admin/users`** — User management (admin only).

### API Endpoints

- **`POST /api/scan`** — Upload a card image (multipart form, field name `card_image`). Accepts JPEG or PNG. Returns `201` with `{ "record_id": N }`.
- **`POST /api/admin/mode`** — Switch extraction mode. Body: `{ "mode": "immediate" }` or `{ "mode": "deferred" }`.
- **`POST /api/admin/trigger`** — Manually trigger extraction of all pending records.
- **`POST /api/admin/requeue`** — Reset all failed records to pending and re-enqueue.
- **`POST /api/admin/requeue/{record_id}`** — Reset a single failed record.
- **`POST /login`** — Authenticate and create session.
- **`GET /logout`** — Invalidate session and redirect to login.
- **`GET /api/admin/users`** — List all users (admin only).
- **`POST /api/admin/users`** — Create user (admin only).
- **`PUT /api/admin/users/{user_id}`** — Update user (admin only).

## Running Multiple Ollama Endpoints

For faster extraction at busy launches, you can distribute work across multiple machines running Ollama. List each in the `extraction_endpoints` array:

```json
"extraction_endpoints": [
  { "url": "http://localhost:11434", "concurrency": 2 },
  { "url": "http://192.168.1.50:11434", "concurrency": 3 }
]
```

The concurrency value controls how many images are sent to that endpoint in parallel. Total worker count equals the sum of all concurrency values.

## HTTPS with Tailscale

Mobile browsers (especially iOS Safari) require HTTPS for camera access. The simplest way to get valid HTTPS certificates for local/home use is through Tailscale's built-in certificate provisioning.

### Prerequisites

- Tailscale installed on the server and on any mobile devices that will scan cards
- All devices logged into the same Tailnet
- MagicDNS enabled in your Tailscale admin console (enabled by default)
- HTTPS certificates enabled in Tailscale admin console: **DNS** → **HTTPS Certificates** → Enable

### Generating Certificates

Run on the server (the machine running Flight Card Scanner):

```bash
tailscale cert $(tailscale status --json | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))")
```

This creates two files in the current directory:
- `<hostname>.crt` — the certificate (signed by Let's Encrypt via Tailscale)
- `<hostname>.key` — the private key

For example, if your machine is `cheshire.neon-tegus.ts.net`:
- `cheshire.neon-tegus.ts.net.crt`
- `cheshire.neon-tegus.ts.net.key`

### Configuring the Application

Add the certificate paths to your `config.json`:

```json
{
  "host": "0.0.0.0",
  "port": 8000,
  "ssl_certfile": "/home/user/FlightCardReader/cheshire.neon-tegus.ts.net.crt",
  "ssl_keyfile": "/home/user/FlightCardReader/cheshire.neon-tegus.ts.net.key",
  ...
}
```

Then start the server:

```bash
python -m flight_card_scanner
```

You'll see:
```
INFO:     SSL enabled — using cert: /home/user/FlightCardReader/cheshire.neon-tegus.ts.net.crt
INFO:     Starting HTTPS server on 0.0.0.0:8000
```

The scan page will automatically generate QR codes with `https://` URLs for Tailscale addresses, making them work with iOS camera access.

### Certificate Renewal

Tailscale certificates are valid for 90 days. When a certificate expires, the server will detect it at startup and fall back to HTTP with a clear warning:

```
WARNING:  SSL disabled — certificate expired on 2025-09-15 12:00 UTC
          Run 'tailscale cert <hostname>' to renew
```

To renew, re-run the `tailscale cert` command and restart the server.

### Troubleshooting Tailscale HTTPS

- **"SSL not configured"** — `ssl_certfile` and/or `ssl_keyfile` are not set in config.json
- **"certificate file not found"** — the path in config.json doesn't point to an existing file
- **"certificate expired"** — run `tailscale cert <hostname>` to get a fresh certificate
- **Phone can't connect** — make sure the phone has Tailscale installed, is logged into the same tailnet, and has the VPN toggle enabled
- **Camera still blocked** — verify you're accessing via the `https://` URL (not `http://`). The QR code on the scan page should show the correct `https://` URL for Tailscale addresses.

## Running Tests

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

## Project Structure

```
FlightCardReader/
├── config.json                        # Application configuration
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
│   │   ├── scan.py                   # POST /scan endpoint
│   │   ├── review.py                 # GET / and GET /record/{id} (HTML)
│   │   ├── admin.py                  # Admin API (mode, trigger, requeue)
│   │   └── auth.py                   # Login, logout, user management
│   ├── services/
│   │   ├── extraction_service.py     # Ollama dispatch, worker pool, date resolution
│   │   ├── image_service.py          # Image storage utilities
│   │   ├── record_service.py         # Database CRUD for flight records
│   │   ├── auth_service.py           # User CRUD, session lifecycle, rate limiting
│   │   └── audit_service.py          # Structured JSON Lines audit logger
│   ├── static/js/                    # Client-side JS (scanner.js, opencv.js)
│   └── templates/                    # Jinja2 HTML templates
├── tests/                            # pytest test suite
├── package.json                      # pnpm package manifest (opencv.js)
└── .venv/                            # Python virtual environment
```

## Troubleshooting

**"Required client-side asset missing: opencv.js"**
Run `pnpm install` from the project root. This installs OpenCV.js into the static assets directory.

**"Ollama returned HTTP 4xx/5xx" or records stuck in `extraction_failed`**
Verify Ollama is running (`curl http://localhost:11434/api/tags`) and that `qwen3-vl` is listed. Re-pull the model if needed: `ollama pull qwen3-vl`.

**Camera not working in the scan UI**
The camera API requires HTTPS on mobile browsers (iOS, Android Chrome). If accessing from a phone on the local network, set up HTTPS via Tailscale (see the "HTTPS with Tailscale" section above). On `localhost` in a desktop browser, HTTP works fine for development.

**Database locked errors**
SQLite supports limited concurrency. For high-volume events, consider running a single extraction worker per endpoint or tuning the `concurrency` values.
