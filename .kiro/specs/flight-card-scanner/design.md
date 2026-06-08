# Design Document: Flight Card Scanner

## Overview

The Flight Card Scanner is a local-network web application that digitises handwritten rocket-launch flight cards at rocketry events. A volunteer holds their phone or laptop camera over a 4.25″ × 5.5″ paper card; the browser automatically detects the card, corrects its perspective, and captures the image. After the volunteer confirms the capture, the image is POSTed to a local Python/FastAPI server that saves the file immediately, creates a database record, and asynchronously submits the image to a local vision-LLM (Qwen2.5-VL via Ollama) for field extraction. A coordinator can browse all scanned records via a server-rendered Review UI.

The entire stack runs offline. No cloud services are involved.

### Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Client framework | Vanilla JS + HTML5 | No build step; easy to serve as static files; OpenCV.js is the heavy dependency |
| Card detection | OpenCV.js (WASM) | GPU-free, in-browser perspective transform; no server round-trip for detection |
| LLM | Qwen2.5-VL via Ollama | Best handwriting OCR in open-weight VLMs; structured JSON output; runs offline |
| Backend | FastAPI (async Python) | Async-first; easy background task management; Pydantic native |
| DB | SQLite via SQLAlchemy async | Zero-admin; single file; sufficient for event-scale (~500–2000 records) |
| Image storage | Filesystem + StaticFiles | Byte-perfect preservation; zero encoding overhead |
| Config | JSON file | Simple; no environment variable leakage; human-editable at an event |
| Asset management | pnpm | Reproducible lockfile; single-command updates |

---

## Architecture

### System Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│  Client (Browser - phone or laptop)                                 │
│                                                                     │
│  ┌──────────────┐    ┌──────────────────┐    ┌──────────────────┐  │
│  │  Camera      │───▶│  Detector        │───▶│  Confirmation    │  │
│  │  Preview     │    │  (OpenCV.js)     │    │  Screen          │  │
│  │  Page        │    │  • contour find  │    │  • show capture  │  │
│  │  getUserMedia│    │  • stability     │    │  • accept/reject │  │
│  └──────────────┘    │  • focus check   │    └────────┬─────────┘  │
│                      │  • perspective   │             │ HTTP POST  │
│                      │    transform     │             │ multipart  │
│                      └──────────────────┘             │            │
└─────────────────────────────────────────────────────────────────────┘
                                                         │
                            ─────────────────────────────
                            │  Local network (WiFi/LAN)
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Server (FastAPI / uvicorn)                                         │
│                                                                     │
│  ┌─────────────┐   ┌─────────────┐   ┌──────────────────────────┐  │
│  │  Scan       │   │  Review     │   │  Admin API               │  │
│  │  Router     │   │  Router     │   │  (mode switch, re-queue) │  │
│  │  POST /scan │   │  GET /      │   │                          │  │
│  └──────┬──────┘   └──────┬──────┘   └──────────┬───────────────┘  │
│         │                 │                      │                  │
│         ▼                 ▼                      ▼                  │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  Service Layer                                               │   │
│  │  • ImageService   • FlightRecordService   • ExtractionService│   │
│  └───────────────────────────────────┬──────────────────────────┘   │
│                                      │                              │
│         ┌────────────────────────────┼────────────────────────┐     │
│         ▼                            ▼                         ▼    │
│  ┌─────────────┐   ┌──────────────────────┐  ┌─────────────────┐   │
│  │  Filesystem │   │  SQLite Database     │  │  Extraction     │   │
│  │  Image Store│   │  (SQLAlchemy async)  │  │  Queue          │   │
│  └─────────────┘   └──────────────────────┘  │  asyncio.Queue  │   │
│                                              └────────┬────────┘   │
│                                                       │             │
└───────────────────────────────────────────────────────┼─────────────┘
                                                        │
                        ────────────────────────────────
                        │  HTTP (local network or localhost)
                        ▼
              ┌──────────────────────┐     ┌──────────────────────┐
              │  Ollama endpoint 1   │     │  Ollama endpoint N   │
              │  (local)             │     │  (remote)            │
              │  concurrency: C1     │     │  concurrency: CN     │
              └──────────────────────┘     └──────────────────────┘
```

### Request / Data Flow: Scan Submission

```
Browser                         FastAPI Server              Ollama
   │                                 │                         │
   │── POST /scan (multipart) ──────▶│                         │
   │                                 │── save image to disk    │
   │                                 │── INSERT FlightRecord   │
   │                                 │   (status=pending)      │
   │◀── 201 {record_id} ────────────│                         │
   │                                 │── background: enqueue   │
   │                                 │   record_id             │
   │                    ┌────────────│                         │
   │                    │ ExtractionWorker picks up            │
   │                    │            │── UPDATE status=process │
   │                    │            │── POST image to Ollama ─│
   │                    │            │                         │── infer
   │                    │            │◀── JSON response ───────│
   │                    │            │── validate + parse JSON │
   │                    │            │── UPDATE status=extract │
   │                    └────────────│   (or extraction_failed)│
```

### Component Map

```
flight_card_scanner/
├── main.py                    # FastAPI app factory, lifespan, startup checks
├── config.py                  # Config dataclass, JSON loader, validation
├── database.py                # SQLAlchemy engine, session factory, Base
├── models.py                  # FlightRecord ORM model
├── schemas.py                 # Pydantic request/response + LLM output schemas
├── routers/
│   ├── scan.py                # POST /scan
│   ├── review.py              # GET /, GET /record/{id}
│   └── admin.py               # POST /admin/mode, POST /admin/requeue,
│                              #   POST /admin/requeue/{id}, POST /admin/trigger
├── services/
│   ├── image_service.py       # Save / retrieve card images
│   ├── record_service.py      # CRUD for FlightRecord
│   └── extraction_service.py  # Queue management, worker pool, Ollama dispatch
├── templates/
│   ├── base.html
│   ├── scan.html              # Camera + detection UI
│   ├── list.html              # Paginated list + search
│   └── detail.html            # Single record detail
└── static/                    # Served by StaticFiles (populated by pnpm)
    └── js/
        ├── scanner.js         # Camera, OpenCV.js pipeline, confirmation
        └── node_modules/      # pnpm-managed (opencv.js, etc.)
```

---

## Components and Interfaces

### Client-Side Architecture

#### Scanning Page (`scan.html` + `scanner.js`)

The scanning page is a single-page experience with two states managed by JavaScript:

**State 1 – Live Preview**
- `<video id="preview">` element streams from `getUserMedia`
- A `<canvas id="overlay">` overlaid on top renders the detected boundary polygon
- Camera-switch `<button id="switchCamera">` enumerates `videoInput` devices and cycles through them

**State 2 – Confirmation Screen**
- The `<video>` and `<canvas>` are hidden
- A `<img id="capturePreview">` shows the captured card (JPEG data URL)
- Accept button, swipe-up gesture listener, and Reject button are shown

#### OpenCV.js Detection Pipeline

The detection pipeline runs inside a `requestAnimationFrame` loop, processing one frame per animation tick. Steps:

```
┌─────────────────────────────────────────────────────────┐
│  captureFrame()                                         │
│  • drawImage(video) onto offscreen canvas               │
│  • cvtColor → GRAY                                      │
│  • GaussianBlur (5×5, σ=0)                              │
│  • Canny edge detection (threshold1=75, threshold2=200) │
│  • findContours(RETR_EXTERNAL, CHAIN_APPROX_SIMPLE)     │
│  • For each contour:                                    │
│    ▸ approxPolyDP (epsilon = 0.02 × arcLength)          │
│    ▸ if result has 4 vertices → candidate               │
│  • Select largest 4-vertex contour by area              │
│  • Area check: contour area / frame area ≥ MIN_FILL     │
└──────────────┬──────────────────────────────────────────┘
               │ card boundary found
               ▼
┌─────────────────────────────────────────────────────────┐
│  stabilityCheck()                                       │
│  • Compare corner positions with previous frame         │
│  • If max corner displacement < STABILITY_THRESHOLD     │
│    for STABILITY_FRAMES consecutive frames → stable     │
└──────────────┬──────────────────────────────────────────┘
               │ card stable
               ▼
┌─────────────────────────────────────────────────────────┐
│  focusCheck()                                           │
│  • Compute Laplacian variance on ROI                    │
│  • If variance ≥ FOCUS_THRESHOLD → in focus             │
└──────────────┬──────────────────────────────────────────┘
               │ in focus
               ▼
┌─────────────────────────────────────────────────────────┐
│  perspectiveTransform()                                 │
│  • Order corners: TL, TR, BR, BL                        │
│  • Compute target dimensions:                           │
│    OUTPUT_W = max(1000, computed_card_width_px)         │
│    OUTPUT_H = max(1300, computed_card_height_px)        │
│  • getPerspectiveTransform → 3×3 matrix                 │
│  • warpPerspective → rectified Mat                      │
│  • imencode → JPEG blob → data URL                      │
└─────────────────────────────────────────────────────────┘
```

**Constants (tunable via `scanner.js`):**

| Constant | Default | Purpose |
|---|---|---|
| `MIN_FILL` | `0.15` | Minimum card-area / frame-area ratio |
| `STABILITY_THRESHOLD` | `10` px | Max corner displacement between frames |
| `STABILITY_FRAMES` | `8` | Consecutive stable frames required |
| `FOCUS_THRESHOLD` | `80.0` | Laplacian variance threshold |
| `OUTPUT_W` | `1000` | Minimum output width (px) |
| `OUTPUT_H` | `1300` | Minimum output height (px) |

#### Camera Switching

```javascript
// scanner.js
async function enumerateCameras(): Promise<MediaDeviceInfo[]>
async function startCamera(deviceId: string | null): Promise<void>
function switchCamera(): Promise<void>   // cycles through enumerated cameras
```

`getUserMedia` is called with `{ video: { deviceId: { exact } } }` when switching. The current device index is persisted in a module-level variable.

#### Confirmation Screen – Gesture Handling

Swipe-up detection uses `touchstart` / `touchend` events on the confirmation image. A vertical delta > 80 px upward triggers accept. The gesture listener is added when entering State 2 and removed on exit.

#### Submission Flow (scanner.js)

```javascript
async function submitCard(jpegDataUrl: string): Promise<void> {
  // 1. Convert data URL → Blob
  // 2. Build FormData with field name "card_image"
  // 3. POST /scan, show spinner
  // 4a. 201 → show record_id, return to live preview
  // 4b. 4xx/5xx → show server error message, offer retry
  // 4c. Network error → show connectivity error, offer retry
}
```

---

### Server Component Structure

#### FastAPI App Factory (`main.py`)

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Load config
    # 2. Validate image store directory (create if absent)
    # 3. Validate / initialise database schema
    # 4. Validate static assets exist (opencv.js etc.)
    # 5. Start extraction worker pool
    # 6. Log configured endpoints + their concurrency limits
    yield
    # 7. Drain extraction queue gracefully

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/images", StaticFiles(directory=config.image_store_path), name="images")
app.include_router(scan_router)
app.include_router(review_router)
app.include_router(admin_router)
```

#### Routers

**`routers/scan.py`**

```python
router = APIRouter()

@router.post("/scan", status_code=201)
async def submit_card(
    card_image: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    config: AppConfig = Depends(get_config),
    extraction_service: ExtractionService = Depends(get_extraction_service),
) -> ScanResponse:
    ...
```

**`routers/review.py`**

```python
router = APIRouter()

@router.get("/", response_class=HTMLResponse)
async def list_records(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    q: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse: ...

@router.get("/record/{record_id}", response_class=HTMLResponse)
async def detail_record(
    request: Request,
    record_id: int,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse: ...
```

**`routers/admin.py`**

```python
router = APIRouter(prefix="/admin")

@router.post("/mode")
async def set_mode(body: SetModeRequest) -> ModeResponse: ...

@router.post("/trigger")
async def trigger_extraction() -> TriggerResponse: ...

@router.post("/requeue")
async def requeue_all_failed(db: AsyncSession = Depends(get_db)) -> RequeueResponse: ...

@router.post("/requeue/{record_id}")
async def requeue_single(record_id: int, db: AsyncSession = Depends(get_db)) -> RequeueResponse: ...
```

#### Background Task Queue / Extraction Worker Pool

The extraction system is built on `asyncio` primitives managed inside the FastAPI lifespan:

```
ExtractionService
├── _mode: ExtractionMode (IMMEDIATE | DEFERRED)
├── _queue: asyncio.Queue[int]          # record IDs
├── _endpoint_semaphores: dict[str, asyncio.Semaphore]
│     # keyed by endpoint URL, size = concurrency limit
└── _workers: list[asyncio.Task]        # one worker per endpoint
```

Each worker is an infinite `asyncio.Task` that:
1. `await self._queue.get()` — blocks until a record ID is available
2. Acquires the endpoint semaphore
3. Fetches the record from DB, sets `status=processing`
4. Calls Ollama API, parses response
5. Writes extracted fields to DB, sets `status=extracted` or `extraction_failed`
6. Releases semaphore, calls `queue.task_done()`

Workers are started in `lifespan` startup and cancelled in the shutdown phase after a `queue.join()` with a timeout.

---

## Data Models

### SQLAlchemy ORM Model

```python
# models.py
from datetime import date, datetime
from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float,
    Integer, String, Text, JSON
)
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass

class FlightRecord(Base):
    __tablename__ = "flight_records"

    # --- Identity ---
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now()
    )

    # --- Image ---
    image_path: Mapped[str] = mapped_column(String(512), nullable=False)

    # --- Extraction lifecycle ---
    extraction_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", index=True
    )
    # Valid values: "pending" | "processing" | "extracted" | "extraction_failed"

    # --- Dedicated extracted columns ---
    flight_date: Mapped[date | None]       = mapped_column(Date, nullable=True)
    flier_name: Mapped[str | None]         = mapped_column(String(256), nullable=True)
    total_impulse_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_impulse_unit: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # total_impulse_unit values: "Ns" | "LbsFt"

    flag_heads_up: Mapped[bool | None]     = mapped_column(Boolean, nullable=True)
    flag_first_flight: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    flag_complex: Mapped[bool | None]      = mapped_column(Boolean, nullable=True)

    rack: Mapped[str | None]               = mapped_column(String(64), nullable=True)
    pad: Mapped[int | None]                = mapped_column(Integer, nullable=True)
    fso_rso_initials: Mapped[str | None]   = mapped_column(String(16), nullable=True)

    evaluation_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # evaluation_outcome values: "good" | "motor" | "airframe" | "recovery"
    evaluation_comments: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- JSON overflow for remaining fields ---
    overflow: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # overflow schema (all keys optional):
    # {
    #   "membership": {
    #     "club": "TRA" | "NAR" | "CAR",
    #     "member_number": str,
    #     "cert_level": int   # 0-3 for TRA/NAR; 0-4 for CAR
    #   },
    #   "rocket_name": str,
    #   "rocket_manufacturer": str,
    #   "rocket_colors": list[str],
    #   "rocket_measurements": {
    #     "diameter": float | null,
    #     "diameter_unit": "in" | "mm" | "cm" | null,
    #     "length": float | null,
    #     "length_unit": "in" | "cm" | null,
    #     "weight": float | null,
    #     "weight_unit": "oz" | "g" | "kg" | "lb" | null
    #   },
    #   "motors": [          # list indexed by stage (index 0 = stage 1)
    #     [                  # list of motors in this stage
    #       {
    #         "manufacturer": str | null,
    #         "leading_number": str | null,   # CTI style prefix e.g. "54"
    #         "letter": str,                  # e.g. "M"
    #         "number": str,                  # e.g. "2560"
    #         "suffix": str | null            # e.g. "WT" or "-P"
    #       }
    #     ]
    #   ],
    #   "notes": str,
    #   "raw_flight_date": str   # stored if date resolution fails
    # }
```

### Database Indexes

```sql
CREATE INDEX ix_flight_records_extraction_status
    ON flight_records (extraction_status);

CREATE INDEX ix_flight_records_created_at
    ON flight_records (created_at DESC);

-- Full-text-style: SQLite doesn't have FTS on JSON; search handled in Python
-- or via LIKE on flier_name column for the list view filter.
```

### Pydantic Schemas (`schemas.py`)

#### LLM Structured Output Schema

This schema is passed to Ollama as the `format` JSON Schema parameter to enforce structured output:

```python
from pydantic import BaseModel, Field
from typing import Optional

class MembershipInfo(BaseModel):
    club: Optional[str] = Field(None, description="TRA, NAR, or CAR")
    member_number: Optional[str] = None
    cert_level: Optional[int] = Field(None, ge=0, le=4)

class RocketMeasurements(BaseModel):
    diameter: Optional[float] = None
    diameter_unit: Optional[str] = None
    length: Optional[float] = None
    length_unit: Optional[str] = None
    weight: Optional[float] = None
    weight_unit: Optional[str] = None

class MotorEntry(BaseModel):
    manufacturer: Optional[str] = None
    leading_number: Optional[str] = None   # CTI prefix e.g. "54"
    letter: str                             # e.g. "M"
    number: str                             # e.g. "2560"
    suffix: Optional[str] = None           # e.g. "WT", "-P", "/180"

class FlightCardExtraction(BaseModel):
    """Structured output schema for Qwen2.5-VL extraction."""
    flight_date_raw: Optional[str] = Field(
        None,
        description=(
            "The flight date exactly as written or circled on the card. "
            "May be a day-of-week name (e.g. 'Saturday') from a pre-printed list that was circled, "
            "a numeric date (e.g. '7/19'), or a full date. "
            "Treat a circled pre-printed day name the same as a handwritten day name."
        )
    )
    flier_name: Optional[str] = None
    membership: Optional[MembershipInfo] = None
    rocket_name: Optional[str] = None
    rocket_manufacturer: Optional[str] = None
    rocket_colors: Optional[list[str]] = None
    measurements: Optional[RocketMeasurements] = None
    motors: Optional[list[list[MotorEntry]]] = Field(
        None,
        description="Outer list = stages (index 0 = stage 1). Inner list = motors in that stage."
    )
    total_impulse_value: Optional[float] = None
    total_impulse_unit: Optional[str] = Field(
        None, description="'Ns' or 'LbsFt'"
    )
    notes: Optional[str] = None
    flag_heads_up: Optional[bool] = None
    flag_first_flight: Optional[bool] = None
    flag_complex: Optional[bool] = None
    rack: Optional[str] = None
    pad: Optional[int] = None
    fso_rso_initials: Optional[str] = None
    evaluation_outcome: Optional[str] = Field(
        None,
        description=(
            "One of: good, motor, airframe, recovery. "
            "May be a circled pre-printed word on the card rather than handwritten text. "
            "Treat a circled pre-printed outcome word as the selected value."
        )
    )
    evaluation_comments: Optional[str] = None
```

#### API Request/Response Schemas

```python
class ScanResponse(BaseModel):
    record_id: int
    message: str = "Card received"

class SetModeRequest(BaseModel):
    mode: str  # "immediate" | "deferred"

class ModeResponse(BaseModel):
    mode: str
    message: str

class TriggerResponse(BaseModel):
    dispatched: int   # number of records enqueued

class RequeueResponse(BaseModel):
    requeued: int     # number of records reset to pending

class FlightRecordSummary(BaseModel):
    id: int
    flier_name: Optional[str]
    rocket_name: Optional[str]      # from overflow
    motor_designation: Optional[str]  # human-readable, derived
    flight_date: Optional[date]
    created_at: datetime
    extraction_status: str

class FlightRecordDetail(BaseModel):
    # All FlightRecord columns + computed fields
    id: int
    image_url: str           # URL to static-served image
    extraction_status: str
    flight_date: Optional[date]
    flier_name: Optional[str]
    total_impulse_value: Optional[float]
    total_impulse_unit: Optional[str]
    flag_heads_up: Optional[bool]
    flag_first_flight: Optional[bool]
    flag_complex: Optional[bool]
    rack: Optional[str]
    pad: Optional[int]
    fso_rso_initials: Optional[str]
    evaluation_outcome: Optional[str]
    evaluation_comments: Optional[str]
    overflow: Optional[dict]
    created_at: datetime
```

---

## Detailed Component Design

### Configuration

#### Config File Schema

```json
{
  "host": "0.0.0.0",
  "port": 8000,
  "image_store_path": "./images",
  "db_path": "./flight_cards.db",
  "event_name": "SCORE Spring Launch 2025",
  "event_date_range": {
    "start": "2025-07-18",
    "end": "2025-07-20"
  },
  "extraction_mode": "immediate",
  "extraction_endpoints": [
    { "url": "http://localhost:11434", "concurrency": 2 },
    { "url": "http://192.168.1.50:11434", "concurrency": 3 }
  ]
}
```

#### Config Dataclass and Loader

```python
# config.py
from dataclasses import dataclass, field
from datetime import date
import json
from pathlib import Path

@dataclass
class EndpointConfig:
    url: str
    concurrency: int = 1

@dataclass
class DateRange:
    start: date
    end: date

@dataclass
class AppConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    image_store_path: Path = Path("./images")
    db_path: Path = Path("./flight_cards.db")
    event_name: str = "Flight Card Scanner"
    event_date_range: DateRange = field(
        default_factory=lambda: DateRange(
            start=date.today(), end=date.today()
        )
    )
    extraction_mode: str = "immediate"       # "immediate" | "deferred"
    extraction_endpoints: list[EndpointConfig] = field(
        default_factory=lambda: [EndpointConfig(url="http://localhost:11434", concurrency=1)]
    )

def load_config(path: Path) -> AppConfig:
    """Load, parse, and validate config from JSON file.
    Logs defaults for any missing keys. Raises ConfigError on invalid values."""
    ...
```

#### Startup Validation Logic

```python
# main.py (inside lifespan)
async def startup_checks(config: AppConfig) -> None:
    # 1. Image store
    config.image_store_path.mkdir(parents=True, exist_ok=True)
    assert os.access(config.image_store_path, os.W_OK), "Image store not writable"

    # 2. Database
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 3. Static assets
    required_assets = ["static/js/opencv.js"]
    for asset in required_assets:
        if not Path(asset).exists():
            logger.error(f"Missing required asset: {asset}. Run `pnpm install`.")
            sys.exit(1)

    # 4. Log endpoints
    for ep in config.extraction_endpoints:
        logger.info(f"Extraction endpoint: {ep.url} (concurrency={ep.concurrency})")
```

---

### Extraction Pipeline

#### Extraction Service

```python
# services/extraction_service.py
import asyncio
from enum import Enum

class ExtractionMode(str, Enum):
    IMMEDIATE = "immediate"
    DEFERRED  = "deferred"

class ExtractionService:
    def __init__(self, config: AppConfig, session_factory):
        self._mode = ExtractionMode(config.extraction_mode)
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._session_factory = session_factory
        self._endpoints = config.extraction_endpoints
        self._workers: list[asyncio.Task] = []

    async def start(self) -> None:
        """Called in lifespan startup. Spawns one worker per endpoint."""
        for ep in self._endpoints:
            sem = asyncio.Semaphore(ep.concurrency)
            for _ in range(ep.concurrency):
                task = asyncio.create_task(
                    self._worker(ep, sem), name=f"extractor-{ep.url}"
                )
                self._workers.append(task)

    async def stop(self) -> None:
        """Called in lifespan shutdown. Drains queue, cancels workers."""
        await asyncio.wait_for(self._queue.join(), timeout=30.0)
        for w in self._workers:
            w.cancel()

    async def enqueue(self, record_id: int) -> None:
        """Enqueue a record for extraction (immediate mode only)."""
        if self._mode == ExtractionMode.IMMEDIATE:
            await self._queue.put(record_id)

    async def set_mode(self, mode: ExtractionMode) -> None:
        """Switch mode. If switching to IMMEDIATE, drain pending records."""
        old_mode = self._mode
        self._mode = mode
        if old_mode == ExtractionMode.DEFERRED and mode == ExtractionMode.IMMEDIATE:
            await self.trigger_pending()

    async def trigger_pending(self) -> int:
        """Enqueue all pending records regardless of mode. Returns count."""
        async with self._session_factory() as db:
            records = await record_service.get_by_status(db, "pending")
            for r in records:
                await self._queue.put(r.id)
            return len(records)

    async def _worker(self, endpoint: EndpointConfig, sem: asyncio.Semaphore) -> None:
        """Infinite worker loop for a single endpoint."""
        async with httpx.AsyncClient(base_url=endpoint.url, timeout=120.0) as client:
            while True:
                record_id = await self._queue.get()
                async with sem:
                    await self._process(record_id, client, endpoint.url)
                self._queue.task_done()

    async def _process(self, record_id: int, client: httpx.AsyncClient, endpoint_url: str) -> None:
        async with self._session_factory() as db:
            record = await record_service.get(db, record_id)
            if record is None:
                return
            await record_service.set_status(db, record_id, "processing")

        try:
            extracted = await self._call_ollama(client, record.image_path)
            resolved_date = resolve_flight_date(extracted.flight_date_raw, config.event_date_range)
            async with self._session_factory() as db:
                await record_service.apply_extraction(db, record_id, extracted, resolved_date)
        except OllamaUnavailableError as e:
            logger.error(f"Endpoint {endpoint_url} unreachable for record {record_id}: {e}")
            async with self._session_factory() as db:
                await record_service.set_status(db, record_id, "extraction_failed")
        except ExtractionParseError as e:
            logger.error(f"Bad JSON from LLM for record {record_id}: {e.raw_response}")
            async with self._session_factory() as db:
                await record_service.set_status(db, record_id, "extraction_failed")
```

#### Ollama API Call

```python
async def _call_ollama(
    self, client: httpx.AsyncClient, image_path: str
) -> FlightCardExtraction:
    """Submit card image to Ollama and return parsed extraction."""
    image_bytes = Path(image_path).read_bytes()
    b64_image = base64.b64encode(image_bytes).decode()

    payload = {
        "model": "qwen2.5-vl",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"}
                    },
                    {
                        "type": "text",
                        "text": EXTRACTION_PROMPT
                    }
                ]
            }
        ],
        "format": FlightCardExtraction.model_json_schema(),
        "stream": False,
        "options": {"temperature": 0}
    }

    resp = await client.post("/api/chat", json=payload)
    resp.raise_for_status()
    data = resp.json()
    raw_content = data["message"]["content"]

    try:
        return FlightCardExtraction.model_validate_json(raw_content)
    except ValidationError:
        raise ExtractionParseError(raw_response=raw_content)
```

#### Extraction Prompt

```python
EXTRACTION_PROMPT = """\
You are an expert data-entry assistant reading a handwritten rocketry flight card.
Extract every readable field from the card image and return them as a JSON object.
Use null for any field that is absent, illegible, or not present on this card.
Do not invent or infer values — only transcribe what is physically written or marked on the card.

IMPORTANT: Some fields use pre-printed options that the flier selects by circling one of the words.
Treat a circled pre-printed word exactly as if the flier had written that word. Specifically:
- Flight date: some cards pre-print the days of the week; a circled day name is the flight date.
- Recovery plan: some cards pre-print recovery method options (e.g. "parachute", "streamer",
  "tumble"); a circled option is the recovery method.
- Post-flight evaluation: some cards pre-print outcome options ("good", "motor", "airframe",
  "recovery"); a circled option is the evaluation_outcome value.

Fields to extract:
- flight_date_raw: the date or day-of-week written or circled on the card, exactly as it appears
- flier_name: the name of the person flying the rocket
- membership: club (TRA/NAR/CAR), member_number (may have trailing letter), cert_level (integer)
- rocket_name, rocket_manufacturer, rocket_colors (list of strings)
- measurements: diameter, diameter_unit, length, length_unit, weight, weight_unit
- motors: nested by stage then motor; each motor has manufacturer, leading_number,
          letter (e.g. M), number (e.g. 2560), suffix (e.g. WT or -P or /180)
- total_impulse_value (number), total_impulse_unit (Ns or LbsFt)
- notes: all free-text notes, recovery plan (including circled pre-printed option if present),
         competition notes, tracking info
- flag_heads_up, flag_first_flight, flag_complex: boolean checkboxes
- rack (string or number), pad (integer)
- fso_rso_initials: safety officer initials
- evaluation_outcome: one of good / motor / airframe / recovery
  (may be a circled pre-printed word rather than handwritten)
- evaluation_comments: any comments written in the evaluation section
"""
```

---

### Flight Date Validation Logic

```python
# services/extraction_service.py

def resolve_flight_date(
    raw: str | None,
    date_range: DateRange
) -> date | None:
    """
    Resolve a raw date string from the LLM to a calendar date.

    Cases handled:
    1. None / empty  → return None  (no date written on card)
    2. Day-of-week name (e.g. "Saturday", "Sat") →
         find the day within event_date_range that matches;
         if no match → raise DateResolutionError(raw)
    3. Numeric date string (e.g. "7/19", "19", "07/19/2025") →
         parse to a date; validate it falls within event_date_range;
         if out of range → raise DateResolutionError(raw)
    4. Full ISO date string (e.g. "2025-07-19") →
         parse; validate; raise DateResolutionError if out of range

    Returns the resolved date, or raises DateResolutionError.
    The caller stores the raw value in overflow['raw_flight_date']
    and sets flight_date = null when DateResolutionError is raised.
    """
    if not raw:
        return None

    # Attempt day-of-week resolution
    day_names = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
        "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6
    }
    normalized = raw.strip().lower()
    if normalized in day_names:
        target_weekday = day_names[normalized]
        current = date_range.start
        while current <= date_range.end:
            if current.weekday() == target_weekday:
                return current
            current += timedelta(days=1)
        raise DateResolutionError(raw)

    # Attempt numeric / ISO date parsing
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d", "%d"):
        try:
            parsed = datetime.strptime(raw.strip(), fmt).date()
            # For formats without year, assume event year
            if fmt in ("%m/%d", "%d"):
                parsed = parsed.replace(year=date_range.start.year)
            if date_range.start <= parsed <= date_range.end:
                return parsed
            raise DateResolutionError(raw)
        except ValueError:
            continue

    raise DateResolutionError(raw)
```

---

### Review UI Page Structure

#### List View (`list.html`)

```
┌──────────────────────────────────────────────────────────────────┐
│  <title>{event_name} – Flight Records</title>                    │
│  <h1>{event_name}</h1>                                           │
│                                                                  │
│  ┌── Status Bar ──────────────────────────────────────────────┐  │
│  │  Extraction mode: [IMMEDIATE ▼]    [Trigger All Pending]   │  │
│  │  Totals: 142 extracted | 3 pending | 1 failed | 0 processing│  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌── Search ──────────────────────────────────────────────────┐  │
│  │  [🔍 Search by name, rocket, motor...        ]             │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌── Re-queue Bar (shown only when failed > 0) ───────────────┐  │
│  │  ⚠ 1 record failed extraction.  [Re-queue All Failed]      │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌── Records Table ───────────────────────────────────────────┐  │
│  │  # │ Flier      │ Rocket    │ Motor  │ Date  │ Status │ TS │  │
│  │  ─────────────────────────────────────────────────────── │  │
│  │  1 │ Jane Doe   │ Mongoose  │ M2560  │ Sat   │ ✅     │ … │  │
│  │  2 │ Bob Smith  │ Aerobee   │ L1150  │ Fri   │ ⏳     │ … │  │
│  │  … │            │           │        │       │        │   │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ← Prev  Page 1 of 3  Next →                                    │
└──────────────────────────────────────────────────────────────────┘
```

Status badge legend: ✅ extracted | ⏳ pending | 🔄 processing | ❌ extraction_failed

#### Detail View (`detail.html`)

```
┌──────────────────────────────────────────────────────────────────┐
│  ← Back to List                                                  │
│  Record #42 — {flier_name}                                       │
│                                                                  │
│  ┌── Card Image ────────────┐  ┌── Extracted Fields ──────────┐  │
│  │                          │  │  Flight Date: 2025-07-19     │  │
│  │  <img src="/images/..."> │  │  Flier: Jane Doe (TRA #1234) │  │
│  │  (responsive, max 50vw)  │  │  Rocket: Mongoose            │  │
│  │                          │  │  Motor(s): AT M2560-WT       │  │
│  └──────────────────────────┘  │  Total Impulse: 2560 Ns      │  │
│                                │  Rack: 3  Pad: 12            │  │
│                                │  FSO: KW                     │  │
│                                │  Outcome: good               │  │
│                                │  Heads Up: ☑ First: ☐ Complex: ☐ │
│                                │  Notes: ...                  │  │
│                                └──────────────────────────────┘  │
│                                                                  │
│  Status: ✅ extracted                                            │
│  Created: 2025-07-19 14:23:07 UTC                               │
│                                                                  │
│  [Re-queue]   ← only shown when status = extraction_failed       │
└──────────────────────────────────────────────────────────────────┘
```

#### Search Implementation

Search is implemented server-side in `routers/review.py` using SQLAlchemy:

```python
async def search_records(
    db: AsyncSession,
    q: str,
    page: int,
    page_size: int
) -> tuple[list[FlightRecord], int]:
    """Search flight_records by flier_name (SQL LIKE) or full Python-side
    JSON overflow scan for rocket_name and motor designation.

    For SQLite, a two-phase approach is used:
    1. SQL query: WHERE flier_name LIKE :q (fast, indexed-friendly)
    2. Python filter: additionally match rocket_name and motor string
       from the overflow JSON if the SQL step yields < page_size results.
    """
    ...
```

Motor designation is rendered as a computed property:

```python
def motor_designation_str(overflow: dict | None) -> str | None:
    """Return a human-readable motor string, e.g. 'AT M2560-WT' or
    '2×AT J450-DMS / AT K600-WT' for clustered/staged flights."""
    ...
```

---

## API Endpoint Contracts

### `POST /scan`

**Purpose:** Submit a captured card image for storage and extraction.

**Request:**
- Content-Type: `multipart/form-data`
- Field: `card_image` (file, required) — JPEG or PNG image bytes

**Response 201:**
```json
{ "record_id": 42, "message": "Card received" }
```

**Response 500:**
```json
{ "detail": "Failed to write image to disk: <error>" }
```

---

### `GET /`

**Purpose:** List view of all flight records.

**Query parameters:**

| Param | Type | Default | Description |
|---|---|---|---|
| `page` | int | 1 | Page number (1-based) |
| `page_size` | int | 50 | Records per page (max 200) |
| `q` | string | — | Search query |

**Response:** HTML (`text/html`)

---

### `GET /record/{record_id}`

**Purpose:** Detail view for a single record.

**Path parameter:** `record_id` (int)

**Response:** HTML (`text/html`)

**Response 404:** HTML error page if record not found.

---

### `POST /admin/mode`

**Purpose:** Switch extraction mode without restart.

**Request body:**
```json
{ "mode": "immediate" }
```
Valid values: `"immediate"` | `"deferred"`

**Response 200:**
```json
{ "mode": "immediate", "message": "Extraction mode set to immediate" }
```

---

### `POST /admin/trigger`

**Purpose:** Manually dispatch all `pending` records for extraction.

**Request body:** (empty)

**Response 200:**
```json
{ "dispatched": 7 }
```

---

### `POST /admin/requeue`

**Purpose:** Reset all `extraction_failed` records to `pending` and dispatch if in immediate mode.

**Response 200:**
```json
{ "requeued": 3 }
```

---

### `POST /admin/requeue/{record_id}`

**Purpose:** Reset a single `extraction_failed` record to `pending`.

**Path parameter:** `record_id` (int)

**Response 200:**
```json
{ "requeued": 1 }
```

**Response 404:** `{ "detail": "Record not found" }`

**Response 422:** `{ "detail": "Record is not in extraction_failed status" }`

---

## Error Handling

### Client-Side Error States

| Situation | Handling |
|---|---|
| Camera permission denied | Static error overlay: "Camera access is required. Please allow camera access in browser settings." |
| `getUserMedia` unsupported | Static error overlay: "This browser does not support camera access. Please use a modern browser." |
| Server 5xx on scan submit | Toast message with server error detail; "Retry" and "Discard" buttons shown |
| Network failure | Toast: "Unable to reach server. Check your network connection." Retry available |
| Server 4xx on scan submit | Toast with detail from JSON `{ "detail": "..." }` |

### Server-Side Error Handling

| Situation | HTTP Status | Behaviour |
|---|---|---|
| Image write failure | 500 | No FlightRecord created; descriptive detail in response |
| DB write failure after image save | 500 | Image file left on disk (orphan); logged; client gets error |
| LLM unavailable during extraction | — | Status → `extraction_failed`; logged with endpoint URL; queue continues |
| LLM returns invalid JSON | — | Status → `extraction_failed`; raw response logged |
| Date resolution failure | — | Status → `extraction_failed`; raw date stored in `overflow.raw_flight_date` |
| Missing config value | — | Default applied and logged at startup; no crash |
| Missing static asset at startup | — | Error logged + `sys.exit(1)` |

### Exception Hierarchy

```python
class FlightCardScannerError(Exception): ...
class ConfigError(FlightCardScannerError): ...
class ImageStorageError(FlightCardScannerError): ...
class ExtractionParseError(FlightCardScannerError):
    def __init__(self, raw_response: str): ...
class OllamaUnavailableError(FlightCardScannerError): ...
class DateResolutionError(FlightCardScannerError):
    def __init__(self, raw_value: str): ...
```

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Image round-trip fidelity

*For any* card image bytes submitted via `POST /scan`, the image file retrieved from the Image Store's static file endpoint SHALL be byte-for-byte identical to the content submitted in the HTTP POST body.

**Validates: Requirements 10.1, 10.2**

---

### Property 2: Atomic submission — image and record created together

*For any* successfully submitted card image that results in an HTTP 201 response, both the image file on disk at `image_path` AND the Flight Record in the database SHALL exist and be consistent (same path), with `extraction_status = "pending"`, before any extraction worker can observe the record.

**Validates: Requirements 4.1, 4.2, 4.3, 6.4**

---

### Property 3: No partial record on write failure

*For any* card image submission that fails to write to the Image Store, the server SHALL return an HTTP 500 response AND no Flight Record SHALL be created in the database for that submission.

**Validates: Requirements 4.5**

---

### Property 4: Non-blocking 201 response

*For any* card image submission, the HTTP 201 response SHALL be returned before any Ollama API call is initiated for the corresponding Flight Record — i.e., the response latency is independent of LLM processing time.

**Validates: Requirements 4.6, 5.1**

---

### Property 5: Processing status set before Ollama call

*For any* Flight Record dispatched for extraction, the `extraction_status` SHALL be updated to `"processing"` in the database before the first byte of the Ollama API request is sent.

**Validates: Requirements 5.2, 6.4**

---

### Property 6: Extraction result persistence — valid and partial responses

*For any* LLM response that conforms to the `FlightCardExtraction` Pydantic schema (including responses where all optional fields are null), after processing completes the Flight Record SHALL have `extraction_status = "extracted"` and every non-null field in the LLM response SHALL be stored in the corresponding database column or overflow JSON key.

**Validates: Requirements 5.4, 5.5, 5.6**

---

### Property 7: Invalid LLM response triggers extraction_failed

*For any* raw string returned by the LLM that fails `FlightCardExtraction.model_validate_json` (i.e., is not valid JSON conforming to the schema), the Flight Record's `extraction_status` SHALL be set to `"extraction_failed"` and no field values from that response SHALL be written to the database.

**Validates: Requirements 5.5, 5.7**

---

### Property 8: Day-of-week date resolution

*For any* event date range and any day-of-week name (case-insensitive, full or abbreviated: Monday–Sunday, Mon–Sun) that names a weekday present in the range, `resolve_flight_date` SHALL return the unique calendar date within that range whose weekday matches the given name.

**Validates: Requirements 5.10, 5.11**

---

### Property 9: Out-of-range date — full failure consequence chain

*For any* raw date string that `resolve_flight_date` cannot resolve to a date within the configured event date range, the resulting Flight Record update SHALL atomically set: `flight_date = null`, `overflow['raw_flight_date'] = raw_string`, and `extraction_status = "extraction_failed"`.

**Validates: Requirements 5.11, 5.12**

---

### Property 10: Extraction status monotonicity

*For any* Flight Record, the `extraction_status` SHALL only advance along the defined lifecycle path (`pending → processing → extracted` OR `pending → processing → extraction_failed`). No status transition outside this DAG SHALL occur except an explicit re-queue call transitioning `extraction_failed → pending`.

**Validates: Requirements 6.4**

---

### Property 11: Re-queue resets to pending and dispatches per mode

*For any* Flight Record in `extraction_failed` status, after a re-queue operation (single-record or bulk) the record's `extraction_status` SHALL be `"pending"`. In Immediate Extraction Mode the record SHALL be present in the extraction queue; in Deferred Extraction Mode it SHALL NOT be in the queue until manually triggered.

**Validates: Requirements 7.7, 7.8, 12.2, 12.3**

---

### Property 12: Mode controls dispatch of new records

*For any* new Flight Record created while in Immediate Extraction Mode, its record ID SHALL appear in the extraction queue. *For any* new Flight Record created while in Deferred Extraction Mode, its record ID SHALL NOT appear in the extraction queue.

**Validates: Requirements 12.2, 12.3**

---

### Property 13: Trigger and mode-switch drain all pending records

*For any* non-empty set of Flight Records with `extraction_status = "pending"`: (a) invoking `trigger_pending` SHALL enqueue all of them, and (b) switching from Deferred to Immediate Extraction Mode SHALL also enqueue all of them — without requiring a separate manual trigger.

**Validates: Requirements 12.7, 12.8**

---

### Property 14: Concurrency limit enforcement per endpoint

*For any* configured Extraction Endpoint with concurrency limit C, the number of simultaneously in-flight Ollama HTTP requests to that endpoint SHALL never exceed C, regardless of queue depth or the number of other configured endpoints.

**Validates: Requirements 13.3, 13.4**

---

### Property 15: Endpoint fault isolation

*For any* multi-endpoint configuration where exactly one endpoint is unreachable, that endpoint's in-progress extraction attempts SHALL be marked `extraction_failed` and that endpoint's failure SHALL not affect the processing of other endpoints or their in-flight extractions.

**Validates: Requirements 13.6**

---

### Property 16: Search result containment

*For any* non-empty search query Q and the set of Flight Records returned by the search, every returned record R SHALL satisfy at least one of: `R.flier_name` contains Q (case-insensitive), `R.overflow['rocket_name']` contains Q (case-insensitive), or `motor_designation_str(R.overflow)` contains Q (case-insensitive).

**Validates: Requirements 7.4**

---

### Property 17: List view renders required fields for every record

*For any* list of Flight Records passed to the list template renderer, every rendered row SHALL contain the flier name, rocket name, motor designation, flight date, and record creation timestamp for that record (or an explicit "—" placeholder where a value is null).

**Validates: Requirements 7.2**

---

### Property 18: Event name appears in every server-rendered page

*For any* configured `event_name` string, every server-rendered HTML page (`/`, `/record/{id}`) SHALL include that string in both the `<title>` element and in a visible heading element.

**Validates: Requirements 9.6**

---

### Property 19: Config loading fidelity

*For any* valid JSON configuration object containing all defined keys, `load_config` SHALL return an `AppConfig` whose fields exactly match the supplied values. *For any* valid JSON configuration object with one or more optional keys absent, `load_config` SHALL return an `AppConfig` with documented default values for the absent keys.

**Validates: Requirements 9.2, 9.3**

---

### Property 20: Perspective transform meets minimum output dimensions

*For any* set of four valid quadrilateral corner points detected in a video frame, the card image produced by `perspectiveTransform` SHALL have width ≥ 1000 pixels and height ≥ 1300 pixels.

**Validates: Requirements 2.3, 2.5**

---

### Property 21: Motor designation rendering completeness

*For any* non-empty `motors` structure (at least one stage with at least one motor entry), `motor_designation_str` SHALL return a non-empty string containing the letter and number of every motor entry, with clustered motors in a stage joined by `×` and stages separated by `/`.

**Validates: Requirements 5.3 (motor sub-field), 7.2**

---

## Testing Strategy

### Dual Testing Approach

Unit tests cover specific examples, edge cases, and error conditions. Property-based tests validate universal properties across generated inputs. Both are needed for comprehensive correctness coverage.

### Property-Based Testing Library

The server is Python; **Hypothesis** is the property-based testing library. Each property test runs a minimum of 100 examples.

Tag format in test files:
```python
# Feature: flight-card-scanner, Property N: <property_text>
```

### Unit Tests

- `test_config.py`: valid config loading, missing-key defaults, invalid extraction mode, invalid date range
- `test_image_service.py`: save + retrieve round trip, unique filename generation, non-writable directory raises `ImageStorageError`
- `test_record_service.py`: create → pending, status transitions, get_by_status
- `test_extraction_service.py`: immediate enqueue, deferred hold, trigger_pending count, mode switch drains pending
- `test_extraction.py`: Ollama mock — valid response → `extracted`, invalid JSON → `extraction_failed`, HTTP error → `extraction_failed`
- `test_date_resolution.py`: day-of-week hit, day-of-week miss, numeric in range, numeric out of range, None input
- `test_motor_designation.py`: single motor, cluster, multi-stage, empty motors
- `test_scan_router.py`: 201 with record_id, 500 on disk failure, file content preserved

### Property-Based Tests (`test_properties.py`)

```python
# Feature: flight-card-scanner, Property 1: Image round-trip fidelity
@given(st.binary(min_size=1, max_size=5_000_000))
def test_image_round_trip(image_bytes): ...

# Feature: flight-card-scanner, Property 2: Atomic submission
@given(st.binary(min_size=100, max_size=1_000_000))
async def test_atomic_submission(image_bytes): ...

# Feature: flight-card-scanner, Property 3: No partial record on write failure
@given(st.binary(min_size=100, max_size=1_000_000))
async def test_no_partial_record_on_failure(image_bytes): ...

# Feature: flight-card-scanner, Property 8: Day-of-week date resolution
@given(st.sampled_from(["Monday","Tuesday","Wednesday","Thursday",
                        "Friday","Saturday","Sunday","Mon","Tue",
                        "Wed","Thu","Fri","Sat","Sun"]),
       st.dates(), st.integers(min_value=1, max_value=7))
def test_day_of_week_resolution(day_name, range_start, range_length): ...

# Feature: flight-card-scanner, Property 9: Out-of-range date consequence chain
@given(st.dates(), st.dates(), st.dates())
def test_out_of_range_consequences(start, end, out_of_range_date): ...

# Feature: flight-card-scanner, Property 11: Re-queue resets to pending and dispatches per mode
@given(st.sampled_from(["immediate", "deferred"]))
async def test_requeue_sets_pending(mode): ...

# Feature: flight-card-scanner, Property 12: Mode controls dispatch of new records
@given(st.sampled_from(["immediate", "deferred"]))
async def test_mode_controls_dispatch(mode): ...

# Feature: flight-card-scanner, Property 13: Trigger and mode-switch drain pending
@given(st.lists(st.integers(min_value=1), min_size=1, max_size=50))
async def test_trigger_drains_pending(record_ids): ...

# Feature: flight-card-scanner, Property 14: Concurrency limit enforcement per endpoint
@given(st.integers(min_value=1, max_value=5), st.integers(min_value=5, max_value=20))
async def test_concurrency_limit(concurrency_limit, num_records): ...

# Feature: flight-card-scanner, Property 16: Search result containment
@given(st.text(min_size=1, max_size=50),
       st.lists(st.builds(FlightRecord, ...), min_size=1, max_size=100))
def test_search_containment(query, records): ...

# Feature: flight-card-scanner, Property 19: Config loading fidelity
@given(st.fixed_dictionaries({
    "host": st.ip_addresses().map(str),
    "port": st.integers(min_value=1024, max_value=65535),
    "event_name": st.text(min_size=1),
    ...
}))
def test_config_loading_fidelity(config_dict): ...

# Feature: flight-card-scanner, Property 20: Perspective transform minimum dimensions
@given(
    # four corner points forming a roughly card-shaped quadrilateral
    st.lists(st.tuples(st.floats(0, 1920), st.floats(0, 1080)), min_size=4, max_size=4)
)
def test_perspective_transform_dimensions(corners): ...

# Feature: flight-card-scanner, Property 21: Motor designation rendering completeness
@given(st.lists(
    st.lists(st.builds(MotorEntry,
                       letter=st.text("ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=1, max_size=1),
                       number=st.text("0123456789", min_size=1, max_size=5)),
             min_size=1), min_size=1))
def test_motor_designation_completeness(motors): ...
```

### Integration Tests

- Start test server with in-memory SQLite
- Submit a real JPEG via `POST /scan`, confirm 201 and record in DB
- Mock Ollama endpoint returns valid JSON; confirm record reaches `extracted` status
- Mock Ollama returns invalid JSON; confirm `extraction_failed`
- Multi-endpoint concurrency: two mock endpoints with limits 1 and 2; submit 9 records; confirm max simultaneous calls per endpoint respected

### Client-Side Tests

- OpenCV.js detection pipeline: not suitable for property-based testing (requires browser WASM runtime)
- Manual test checklist: camera switch, boundary overlay, auto-capture, shutter sound, accept/reject, swipe-up, retry after network error
