"""Scan routers: HTML page (GET /scan) and API endpoint (POST /api/scan).

Handles:
- Serving the scanner camera UI page
- Card image uploads: validates the file type, saves the image,
  creates a FlightRecord, and enqueues the record for extraction.
"""

from __future__ import annotations

import base64
import io
import logging
from datetime import date, timedelta
from pathlib import Path

import segno
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import AppConfig
from ..database import get_db
from ..exceptions import ImageStorageError
from ..schemas import ScanResponse
from ..services import image_service, record_service
from ..services.extraction_service import ExtractionService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dependency helpers (simple module-level state; wired up in main.py lifespan)
# ---------------------------------------------------------------------------

_config: AppConfig | None = None
_extraction_service: ExtractionService | None = None
_templates: Jinja2Templates | None = None


def configure(
    config: AppConfig,
    extraction_service: ExtractionService,
    templates: Jinja2Templates | None = None,
) -> None:
    """Set module-level dependencies. Called once during app startup."""
    global _config, _extraction_service, _templates
    _config = config
    _extraction_service = extraction_service
    _templates = templates


def get_config() -> AppConfig:
    """FastAPI dependency that returns the application config."""
    if _config is None:
        raise RuntimeError("Scan router not configured. Call configure() at startup.")
    return _config


def get_extraction_service() -> ExtractionService:
    """FastAPI dependency that returns the ExtractionService instance."""
    if _extraction_service is None:
        raise RuntimeError("Scan router not configured. Call configure() at startup.")
    return _extraction_service


# ---------------------------------------------------------------------------
# Allowed content types and extensions
# ---------------------------------------------------------------------------

_ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png"}
_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png"}

# Map content type to file extension for save_image
_CONTENT_TYPE_TO_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
}


def _resolve_extension(upload: UploadFile) -> str | None:
    """Determine the file extension from content type or filename.

    Returns the extension (without dot) if the file is an allowed type,
    or None if validation fails.
    """
    # Primary check: content type
    if upload.content_type in _ALLOWED_CONTENT_TYPES:
        return _CONTENT_TYPE_TO_EXT[upload.content_type]

    # Fallback: check filename extension
    if upload.filename:
        suffix = Path(upload.filename).suffix.lower()
        if suffix in _ALLOWED_EXTENSIONS:
            return "jpg" if suffix in {".jpg", ".jpeg"} else "png"

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_event_dates(config: AppConfig) -> list[dict[str, str]]:
    """Build a list of {value, label} dicts for every date in the event range."""
    dates: list[dict[str, str]] = []
    current = config.event_date_range.start
    end = config.event_date_range.end
    while current <= end:
        dates.append({
            "value": current.isoformat(),
            "label": current.strftime("%A %-m/%-d"),
        })
        current += timedelta(days=1)
    return dates


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter()


def _get_all_addresses() -> list[tuple[str, bool]]:
    """Return all non-loopback IP addresses (IPv4 and IPv6) on this host.

    Returns a list of (address, is_tailscale) tuples.
    """
    import subprocess

    addresses: list[tuple[str, bool]] = []

    # Try to get Tailscale DNS hostname
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            import json as _json
            ts_data = _json.loads(result.stdout)
            dns_name = ts_data.get("Self", {}).get("DNSName", "")
            if dns_name:
                # Strip trailing dot
                dns_name = dns_name.rstrip(".")
                addresses.append((dns_name, True))
    except (subprocess.SubprocessError, OSError, FileNotFoundError, ValueError):
        pass

    # Get IPv6 global addresses
    try:
        result = subprocess.run(
            ["ip", "-6", "addr", "show", "scope", "global"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.startswith("inet6 "):
                    addr = line.split()[1].split("/")[0]
                    addresses.append((addr, _is_tailscale_addr(addr)))
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        pass

    # Get IPv4 addresses (non-loopback)
    try:
        result = subprocess.run(
            ["ip", "-4", "addr", "show"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.startswith("inet "):
                    addr = line.split()[1].split("/")[0]
                    if not addr.startswith("127."):
                        addresses.append((addr, _is_tailscale_addr(addr)))
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        pass

    return addresses


def _generate_qr_data_uri(url: str) -> str:
    """Generate a QR code SVG as a data URI for the given URL."""
    qr = segno.make(url)
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=4, border=2)
    svg_bytes = buf.getvalue()
    b64 = base64.b64encode(svg_bytes).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


def _is_tailscale_addr(addr: str) -> bool:
    """Return True if the address belongs to a Tailscale interface.

    Tailscale uses:
    - IPv6 ULA prefix fd7a:115c:a1e0::/48
    - IPv4 CGNAT range 100.64.0.0/10 (100.64-127.x.x.x)
    """
    if addr.startswith("fd7a:115c:a1e0:"):
        return True
    # Tailscale IPv4: 100.64.0.0 - 100.127.255.255
    parts = addr.split(".")
    if len(parts) == 4:
        try:
            first, second = int(parts[0]), int(parts[1])
            if first == 100 and 64 <= second <= 127:
                return True
        except ValueError:
            pass
    return False


def _make_url(addr: str, port: int, is_tailscale: bool = False, ssl_enabled: bool = False) -> str:
    """Build a URL from an IP address and port, bracketing IPv6.

    Uses https:// for Tailscale addresses when SSL is enabled.
    """
    scheme = "https" if (is_tailscale and ssl_enabled) else "http"
    # DNS hostnames (no colons, no dots-only-as-IPv4)
    if ":" in addr:
        return f"{scheme}://[{addr}]:{port}/scan"
    return f"{scheme}://{addr}:{port}/scan"


@router.get("/scan", response_class=HTMLResponse)
async def scan_page(
    request: Request,
    config: AppConfig = Depends(get_config),
) -> HTMLResponse:
    """Serve the scanner camera UI page."""
    if _templates is None:
        raise RuntimeError("Scan router not configured with templates.")

    # Determine if SSL is active (cert files configured and exist)
    ssl_enabled = (
        config.ssl_certfile is not None
        and config.ssl_keyfile is not None
        and config.ssl_certfile.exists()
        and config.ssl_keyfile.exists()
    )

    # Generate QR codes for all available addresses
    address_list = _get_all_addresses()
    qr_entries: list[dict[str, str]] = []
    for addr, is_ts in address_list:
        url = _make_url(addr, config.port, is_tailscale=is_ts, ssl_enabled=ssl_enabled)
        data_uri = _generate_qr_data_uri(url)
        qr_entries.append({"url": url, "qr": data_uri})

    return _templates.TemplateResponse(
        "scan.html",
        {
            "request": request,
            "event_name": config.event_name,
            "qr_entries": qr_entries,
            "event_dates": _build_event_dates(config),
        },
    )


@router.post("/api/scan", status_code=201, response_model=ScanResponse)
async def submit_card(
    card_image: UploadFile = File(...),
    flight_date: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
    config: AppConfig = Depends(get_config),
    extraction_service: ExtractionService = Depends(get_extraction_service),
) -> ScanResponse:
    """Accept a card image upload and create a pending flight record.

    1. Validate file is JPEG or PNG.
    2. Save image to the Image Store.
    3. Create a FlightRecord (status=pending).
    4. Enqueue the record for extraction (no-op in DEFERRED mode).
    5. Return 201 with the record ID.
    """
    # --- 1. Validate file type ---
    ext = _resolve_extension(card_image)
    if ext is None:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Only JPEG and PNG images are accepted.",
        )

    # --- 2. Save image ---
    file_bytes = await card_image.read()
    try:
        filename = image_service.save_image(
            file_bytes=file_bytes,
            ext=ext,
            store_path=config.image_store_path,
        )
    except ImageStorageError as exc:
        logger.error("Failed to save uploaded image: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="Failed to store the uploaded image.",
        ) from exc

    # --- 3. Create DB record ---
    # Parse the optional flight_date override from the form
    parsed_flight_date: date | None = None
    if flight_date:
        try:
            parsed_flight_date = date.fromisoformat(flight_date)
        except ValueError:
            logger.warning("Invalid flight_date value: %r — ignoring", flight_date)

    try:
        record = await record_service.create(
            db, image_path=filename, flight_date=parsed_flight_date
        )
    except Exception as exc:
        # Rollback: delete the saved image since the record wasn't created
        logger.error("Failed to create flight record: %s", exc)
        image_service.delete_image(config.image_store_path / filename)
        raise HTTPException(
            status_code=500,
            detail="Failed to create flight record.",
        ) from exc

    # --- 4. Enqueue for extraction ---
    await extraction_service.enqueue(record.id)

    # --- 5. Return success ---
    return ScanResponse(record_id=record.id)
