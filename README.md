# Flight Card Scanner

A web application for digitizing handwritten rocketry flight cards. Users photograph flight cards using their phone or tablet camera, and the app automatically extracts structured data (flier name, motor designation, flight date, etc.) using an Ollama-hosted vision language model (Qwen2.5-VL).

## How It Works

1. A user opens the web interface on a mobile device and photographs a flight card using the built-in camera UI.
2. The image is uploaded and stored on the server.
3. The server dispatches the image to one or more Ollama endpoints running Qwen2.5-VL for structured data extraction.
4. Extracted data is stored in a SQLite database and viewable through a review interface with search and pagination.

The extraction can run in two modes:
- **Immediate** — extraction begins as soon as a card is scanned.
- **Deferred** — images are queued and extraction is triggered manually via the admin API.

## Prerequisites

- **Ubuntu Linux** (22.04 or later recommended)
- **Python 3.13+**
- **Node.js 18+** and **pnpm** (for client-side OpenCV.js dependency)
- **Ollama** with the `qwen2.5-vl` model pulled and running

### Installing Ollama

Follow the official instructions at [https://ollama.com/download/linux](https://ollama.com/download/linux):

```bash
curl -fsSL https://ollama.ai/install.sh | sh
```

Then pull the vision model:

```bash
ollama pull qwen2.5-vl
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
  "image_store_path": "./images",
  "db_path": "./flight_cards.db",
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
| `image_store_path` | string | `"./images"` | Directory where uploaded card images are saved |
| `db_path` | string | `"./flight_cards.db"` | Path to the SQLite database file |
| `event_name` | string | `"Flight Card Scanner"` | Display name shown in the web UI |
| `event_date_range` | object | today–today | Inclusive start/end dates (ISO 8601) for the launch event. Used to resolve day-of-week dates written on cards. |
| `extraction_mode` | string | `"immediate"` | `"immediate"` or `"deferred"`. Controls whether extraction runs automatically on upload. |
| `extraction_endpoints` | array | localhost:11434, concurrency 1 | List of Ollama endpoints. Each entry has a `url` and a `concurrency` limit (number of parallel requests). |

All keys are optional — defaults are applied for any missing key.

## Running the Application

Make sure Ollama is running, then start the server:

```bash
source .venv/bin/activate
uvicorn flight_card_scanner.main:app --host 0.0.0.0 --port 8000
```

Or use a custom config path:

```bash
CONFIG_PATH=/path/to/my-config.json uvicorn flight_card_scanner.main:app --host 0.0.0.0 --port 8000
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

### API Endpoints

- **`POST /api/scan`** — Upload a card image (multipart form, field name `card_image`). Accepts JPEG or PNG. Returns `201` with `{ "record_id": N }`.
- **`POST /api/admin/mode`** — Switch extraction mode. Body: `{ "mode": "immediate" }` or `{ "mode": "deferred" }`.
- **`POST /api/admin/trigger`** — Manually trigger extraction of all pending records.
- **`POST /api/admin/requeue`** — Reset all failed records to pending and re-enqueue.
- **`POST /api/admin/requeue/{record_id}`** — Reset a single failed record.

## Running Multiple Ollama Endpoints

For faster extraction at busy launches, you can distribute work across multiple machines running Ollama. List each in the `extraction_endpoints` array:

```json
"extraction_endpoints": [
  { "url": "http://localhost:11434", "concurrency": 2 },
  { "url": "http://192.168.1.50:11434", "concurrency": 3 }
]
```

The concurrency value controls how many images are sent to that endpoint in parallel. Total worker count equals the sum of all concurrency values.

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
│   ├── database.py                   # SQLAlchemy async engine/session setup
│   ├── models.py                     # ORM models (FlightRecord)
│   ├── schemas.py                    # Pydantic request/response models
│   ├── exceptions.py                 # Custom exception classes
│   ├── routers/
│   │   ├── scan.py                   # POST /scan endpoint
│   │   ├── review.py                 # GET / and GET /record/{id} (HTML)
│   │   └── admin.py                  # Admin API (mode, trigger, requeue)
│   ├── services/
│   │   ├── extraction_service.py     # Ollama dispatch, worker pool, date resolution
│   │   ├── image_service.py          # Image storage utilities
│   │   └── record_service.py         # Database CRUD for flight records
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
Verify Ollama is running (`curl http://localhost:11434/api/tags`) and that `qwen2.5-vl` is listed. Re-pull the model if needed: `ollama pull qwen2.5-vl`.

**Camera not working in the scan UI**
The camera API requires HTTPS in production (or `localhost` for development). If accessing from another device on the network, you'll need to serve over HTTPS (e.g., behind a reverse proxy with TLS).

**Database locked errors**
SQLite supports limited concurrency. For high-volume events, consider running a single extraction worker per endpoint or tuning the `concurrency` values.
