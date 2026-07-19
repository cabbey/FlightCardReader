# Project Layout Guide

inclusion: auto

## Overview

This is a FastAPI application (`flight_card_scanner`) that serves HTML pages with Jinja2 templates and exposes JSON API endpoints. The frontend is vanilla JavaScript (no build step, no framework).

## Key Directories

```
flight_card_scanner/
├── main.py                  # FastAPI app creation, lifespan, static/template mounting
├── config.py                # AppConfig dataclass, load_config()
├── database.py              # SQLAlchemy async engine/session setup (event DB)
├── auth_database.py         # SQLAlchemy async engine/session for auth DB (separate from event DB)
├── models.py                # SQLAlchemy ORM models (FlightRecord, etc.)
├── auth_models.py           # Auth ORM models (User, Session) with AuthBase
├── schemas.py               # Pydantic response/request schemas
├── auth_schemas.py          # Pydantic schemas for auth endpoints (CreateUser, UpdateUser, UserResponse)
├── exceptions.py            # Custom exception classes
├── dependencies/            # FastAPI dependency factories
│   └── auth.py              # Role enum, require_role() dependency, _is_api_request()
├── middleware/              # ASGI middleware
│   └── session_middleware.py # Cookie-based session resolution (itsdangerous signing)
├── routers/                 # FastAPI route handlers (one file per feature area)
│   ├── scan.py              # GET /scan (camera UI page), POST /api/scan (image upload)
│   ├── review.py            # GET / (record list), GET /card/{id} (detail), PATCH /api/card/{id}
│   ├── admin.py             # Admin/management endpoints
│   ├── auth.py              # GET /login, POST /login, GET /logout, user management API
│   └── reports.py           # GET /reports, GET /reports/day/{date}
├── services/                # Business logic layer (called by routers)
│   ├── image_service.py     # save_image(), delete_image()
│   ├── record_service.py    # CRUD for FlightRecord
│   ├── extraction_service.py # LLM-based card data extraction (Ollama)
│   ├── flier_match_service.py # Fuzzy name matching against known fliers
│   ├── motor_lookup_service.py # ThrustCurve motor database lookups
│   ├── auth_service.py      # AuthService: user CRUD, session lifecycle, rate limiting, IP binding
│   └── audit_service.py     # Structured JSON Lines audit logger (fire-and-forget)
├── templates/               # Jinja2 HTML templates
│   ├── base.html            # Base layout (nav, shared CSS)
│   ├── scan.html            # Camera capture UI + confirmation/review screen
│   ├── list.html            # Flight record list (home page)
│   ├── detail.html          # Single flight record detail/edit
│   ├── reports.html         # Reports index
│   ├── report_day.html      # Per-day report view
│   ├── login.html           # Login form
│   ├── users.html           # Admin user management page
│   └── 404.html             # Not found page
└── static/
    └── js/
        ├── scanner.js       # All client-side scanning logic (camera, OpenCV detection,
        │                    #   perspective transform, confirmation screen, submission)
        └── node_modules/    # Vendored JS deps (opencv.js)
```

## Routing & API Patterns

- **HTML pages** are served via `GET` routes that return `TemplateResponse`.
- **API endpoints** live under `/api/` (e.g., `POST /api/scan`, `PATCH /api/card/{id}`).
- Each router file calls `configure()` at startup to receive the `AppConfig`, services, and templates.
- Dependencies are injected via FastAPI `Depends()` (config, db session, services).

## Authentication & Authorization

- **Session middleware** (`middleware/session_middleware.py`) resolves signed cookies (itsdangerous `URLSafeSerializer`) → attaches user to `request.state.user` (or `None` if unauthenticated/expired).
- **`require_role(min_role)`** dependency factory (`dependencies/auth.py`) enforces access control:
  - API requests (`/api/` prefix or `Accept: application/json`) → 401 Unauthorized if not authenticated, 403 Forbidden if insufficient role.
  - HTML requests → 302 redirect to `/login` if not authenticated, 403 if insufficient role.
- **Role hierarchy:** `ADMIN` (2) > `DATA_ENTRY` (1) > `PUBLIC` (0). A user with a higher role automatically satisfies lower role requirements.
- **Template conditional rendering:** Templates use `{% if can_mutate %}` and `{% if is_admin %}` for server-side element exclusion (buttons, forms, admin links).
- **Audit logging:** `services/audit_service.py` provides `log_action(actor, action, object_type, object_id, details)` — fire-and-forget JSON Lines written to disk. Never logs plaintext passwords.

## Frontend Architecture

- No JS framework — plain vanilla JS wrapped in an IIFE.
- `scanner.js` handles the entire scan page: camera access, OpenCV.js card detection, perspective correction, confirmation/review screen, rotation, zoom, swipe gestures, and submission.
- Inline `<script>` blocks in `scan.html` handle simple UI toggles (mirror, debug, QR switcher).
- Templates extend `base.html` which provides shared nav and CSS.
- Styles are defined inline in each template's `{% block styles %}`.

## Configuration

- Runtime config lives in `config.json` at the project root (loaded by `config.py`).
- Detection pipeline constants (MIN_FILL, OUTPUT_W/H, stability, focus thresholds) are defined as JS variables at the top of `scanner.js`.
- The "Relaxed" mode checkbox toggles wider detection tolerances client-side (no server config needed).
- **Auth config:** `auth_db_path`, `session_timeout_hours`, `audit_log_path` in `config.json`.
- **Environment variables:** `FCS_SESSION_SECRET` (required, ≥16 chars for cookie signing), `FCS_ADMIN_EMAIL` and `FCS_ADMIN_PASSWORD` (optional, auto-creates admin on first run).

## Common Modification Patterns

| Task | Files to touch |
|------|---------------|
| Change scan UI appearance | `templates/scan.html` (styles + markup) |
| Change detection/capture behavior | `static/js/scanner.js` (constants or pipeline functions) |
| Add a new API endpoint | `routers/<area>.py` + `schemas.py` (if new response type) |
| Add a new page | `routers/<area>.py` + `templates/<name>.html` |
| Change how records are stored/queried | `models.py` + `services/record_service.py` |
| Change image processing | `services/image_service.py` |
| Change extraction (LLM) logic | `services/extraction_service.py` |
| Add config options | `config.py` (AppConfig) + `config.json` |
| Change auth/session behavior | `services/auth_service.py` + `middleware/session_middleware.py` |
| Change role permissions | `dependencies/auth.py` + router decorators |
| Change audit logging | `services/audit_service.py` |
| Add/modify user management | `routers/auth.py` + `auth_schemas.py` + `templates/users.html` |
| Protect a new endpoint | Add `dependencies=[Depends(require_role(Role.X))]` to route decorator |
