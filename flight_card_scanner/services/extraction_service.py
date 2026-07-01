"""Extraction queue management, worker pool, and Ollama dispatch.

Provides:
- ``ExtractionService`` — async worker pool managing Ollama dispatch
- ``ExtractionMode`` — enum for IMMEDIATE vs DEFERRED operation
- ``resolve_flight_date`` — resolves raw LLM date strings to calendar dates
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path

import httpx
from pydantic import ValidationError

from flight_card_scanner.config import AppConfig, DateRange, EndpointConfig
from flight_card_scanner.exceptions import (
    DateResolutionError,
    ExtractionParseError,
    OllamaUnavailableError,
)
from flight_card_scanner.schemas import FlightCardExtraction

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Extraction Prompt
# ---------------------------------------------------------------------------

EXTRACTION_PROMPT = """
You are an expert data-entry assistant reading a handwritten rocketry flight card.
Extract every readable field from the card image and return them as a JSON object.
Use null for any field that is absent, illegible, or not present on this card.
Do not invent values that have no basis on the card. However, you SHOULD apply domain knowledge
to correct obvious handwriting misreads and ambiguous characters — for example, interpreting "O"
as "0" in a numeric field, or reading "I218R" instead of "I2/8R" for a motor designation. Use
the format expectations described below to guide your interpretation of ambiguous handwriting.

IMPORTANT — HOW USERS SELECT PRE-PRINTED OPTIONS:
Many fields on these cards have pre-printed options. Users indicate their selection by:
- CIRCLING the chosen word/option (most common)
- UNDERLINING the chosen word/option
- Putting a CHECK MARK next to the option
Any of these markings means that option is the selected value. Treat them identically.
Do NOT interpret circling as parentheses around text. If you see what looks like "(Sun)" on a
line of pre-printed day names, that is usually "Sun" circled — the selected value is "Sun".
Do NOT default to the first option in a list. If no option is clearly marked, use null.

IMPORTANT — SELECTION BIAS WARNING:
When multiple options are pre-printed (e.g. "NAR TRA CAR" or "Fri Sat Sun"), you MUST
carefully look for which specific option has a circle, underline, or check mark.
Do NOT assume the first item is selected. If "TRA" is circled/underlined, the value is "TRA"
even though "NAR" appears first in the list. Look at the hand drawn ink marks, not the position.

Fields to extract:

- flight_date_raw: the date or day-of-week written or circled/underlined on the card, exactly
  as it appears. Some cards pre-print days of the week; a circled or underlined day name is the
  flight date. Many cards also have an expiration date for club membership below, be sure not to
  use the expiration date as the flight date.
  CONTEXT: This event runs from {event_start} to {event_end}. All flights occurred within this
  date range. If you read a numeric date that seems impossible (e.g. "36" for a day in April),
  consider that sloppy handwriting may be the cause — "36" is likely "26", "31" might be "21",
  etc. Apply reasonable corrections when the literal reading would be an invalid date but a
  similar-looking digit gives a valid date within the event range. If there is any conflict
  between a written numeric date and a selected day of week, use the day of week. 

- flier_name: the name of the person flying the rocket

- membership:
  - member_number: This is ALWAYS a numeric string (digits only, possibly with a trailing
    letter suffix like "12345A"). It is a membership ID number. When reading handwriting in
    this field, strongly prefer digit interpretations: O→0, I→1, l→1, S→5, B→8, R is NOT
    valid — if you see what looks like "R" consider it might be "12" written together.
    The typical format is 4-6 digits, optionally followed by a single letter.

- rocket_name, rocket_manufacturer, rocket_colors (list of strings)
  Note: "Scratch" is a common manufacturer value meaning the rocket was scratch-built
  (designed and built by the flier, not from a kit). Treat it as a valid manufacturer name.
  Other common manufacturers are: Estes, Wildman, LOC, LOC Precision, Aerotech, MadCow, Missle
  Works, MAC Performance, Binder, and Apogee. These fields are not super important, just take
  whatever looks right at first, do not put much effort into it.

- measurements: diameter, diameter_unit, length, length_unit, weight, weight_unit

- motors: A flat array of motor objects used in this flight (most cards have just one).
  Each motor object has: manufacturer, leading_number, letter, number, suffix.
  MOTOR DESIGNATION FORMAT: A motor designation follows a strict pattern:
    [leading_number]<letter><number>[-suffix]
  Where:
  - leading_number (optional, rare): the total thrust in Newtons, ALWAYS a pure integer (no slashes, no decimals).
  - letter: a SINGLE uppercase letter (A through O) indicating the total impulse class.
    Common letters: A, B, C, D, E, F, G, H, I, J, K, L, M, N, O
  - number: the average thrust in Newtons, ALWAYS a pure integer (no slashes, no decimals).
    Examples: 218, 1000, 2560, 450, 65, 180
  - suffix (optional): a code for propellant type like "WT", "R", "P", "DMS", "SS", "FJ" may be
    listed after a space or a hyphen. In smaller motors, this may also be numeric to infidate a
    delay time.
  
  CRITICAL: The letter+number portion has NO separator between them. "I218" is correct
  (letter=I, number=218). If you see what looks like "I2/8" or "I2|8", that is almost
  certainly "I218" with a misread — the slash or dash is actually part of a digit.
  The number is always an integer: 218, not 2/8 or 2.8.
  
  Examples of valid motor designations:
  - "C6-7"  → letter=C, number=6, suffix=7
  - "H128W" → letter=H, number=128, suffix=W
  - "I218R" → letter=I, number=218, suffix=R
  - "J450DMS" → letter=J, number=450, suffix=DMS
  - "54M2560WT" → leading_number=54, letter=M, number=2560, suffix=WT
  - "K600" → letter=K, number=600 (no suffix)
  
  Common manufacturer prefixes (sometimes written before the designation, space-separated):
  AT (Aerotech), CTI (Cesaroni), AMW (Animal Motor Works), Loki, Estes, Q-jet, Quest, Sugar,
  Experimental, or Research

- total_impulse_value (number), total_impulse_unit (Ns or LbsFt)
  ONLY fill this in if there is a dedicated field/line on the card for total impulse.
  Do NOT calculate or infer it from the motor designation. Many low-power flight cards
  do not have this field at all — use null in that case.

- recovery_plan: The recovery method for this flight. Often pre-printed options like
  "parachute", "streamer", "tumble", "dual deploy", "none" that the user circles or
  underlines. May also be handwritten. This is a SEPARATE field from notes — do NOT merge
  recovery plan information into the notes field.
  IMPORTANT: When you see patterns like "_____ @ ______", the "@" symbol
  means (recovery type) "at" (deployment altitude/event). The value before "@" is usually 
  a type of recovery like "main" or "drogue" for types of parachute, or other recovery methods
  like "streamer", "tumble", or "chute release". The value after "@" is almost always "apogee" or an
  altitude measurement like "500m", "1000'", "800ft", "300m AGL". These are recovery deployment
  events, NOT email addresses. For example "main @ 700'" means "main parachute deploys at 700
  feet". "drogue @ apogee" means "drogue deploys at apogee". Transcribe these exactly as written.

- notes: Free-text notes, competition notes, tracking info. Do NOT include recovery plan
  here — that goes in the recovery_plan field above.
  Same as recovery_plan: if you see "<event> @ <value>" patterns in notes, this is unlikely to
  be an email address.

- flag_heads_up, flag_first_flight, flag_complex: These are CHECKBOX fields (boolean).
  CRITICAL: The checkbox or check area is ALWAYS an outlined shape adjacent to its text label,
  it is never a label for a line of text to be written in. A checkbox is true ONLY if there
  is a check mark, X, slash, or any mark IN or through the checkbox shape, or if the shape is
  filled in entirely. Do NOT interpret any hand writing near the label as indicating that checkbox
  is checked — that writing probably belongs to a different field (often fso_rso_initials is
  written to the right of the checkboxes area). If there is no clear mark in the checkbox area,
  the value is false.

- rack (string or number), pad (integer)
  If the card contains the words "low power flight card" and the rack field is blank/empty,
  fill in rack with "L". The rack should be an integer less than 6 or the letters "L", "LP",
  "Low", or "Low Power"

- fso_rso_initials: safety officer initials. These are often written to the RIGHT of the
  checkbox area or in a dedicated "RSO" or "FSO" field. Do not confuse these initials with
  checkbox markings near them.

- evaluation_outcome: one of good / motor / airframe / recovery.
  Usually pre-printed options that the user circles or underlines. Look for which specific
  word is marked — do not default to the first option. Some flight cards use older terms:
  "shred" means "airframe", and "cato" means "motor". Do not just default to the first
  pre-printed value if there is no hand written indication that it was selected.

- evaluation_comments: any comments written in the evaluation section
"""


# ---------------------------------------------------------------------------
# Schema simplification for constrained decoding
# ---------------------------------------------------------------------------


def _simplify_schema(schema: dict) -> dict:
    """Simplify a Pydantic v2 JSON schema for better LLM constrained decoding.

    Pydantic v2 emits `anyOf: [{type: X}, {type: null}]` for Optional fields,
    which many constrained decoding engines struggle with. This replaces those
    patterns with a simpler `type: X`. Also inlines $ref definitions to avoid
    reference resolution issues.
    """
    import copy
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", {})
    # Simplify anyOf in definitions first
    for defn in defs.values():
        _simplify_node(defn)
    # Simplify the top-level schema
    _simplify_node(schema)
    # Inline all $ref references
    _inline_refs(schema, defs)
    return schema


def _simplify_node(node: dict) -> None:
    """Recursively simplify anyOf patterns in a schema node."""
    if not isinstance(node, dict):
        return

    props = node.get("properties", {})
    for key, prop in props.items():
        if "anyOf" in prop:
            # Check if it's the common Optional pattern: [{type: X}, {type: null}]
            any_of = prop["anyOf"]
            non_null = [t for t in any_of if t != {"type": "null"}]
            has_null = {"type": "null"} in any_of
            if has_null and len(non_null) == 1:
                # Replace anyOf with the non-null type
                simple_type = non_null[0]
                prop.pop("anyOf")
                prop.update(simple_type)
        # Recurse into nested objects
        if "properties" in prop:
            _simplify_node(prop)
        if "items" in prop:
            if isinstance(prop["items"], dict):
                _simplify_node(prop["items"])


def _inline_refs(node, defs: dict) -> None:
    """Recursively inline all $ref references using the provided definitions."""
    if isinstance(node, dict):
        for key in list(node.keys()):
            if key == "$ref" and isinstance(node["$ref"], str):
                ref_name = node["$ref"].split("/")[-1]
                if ref_name in defs:
                    # Replace the $ref with the inlined definition
                    node.pop("$ref")
                    import copy
                    node.update(copy.deepcopy(defs[ref_name]))
            else:
                _inline_refs(node[key], defs)
    elif isinstance(node, list):
        for item in node:
            _inline_refs(item, defs)


# ---------------------------------------------------------------------------
# Extraction Mode
# ---------------------------------------------------------------------------


class ExtractionMode(str, Enum):
    """Server extraction operating mode."""

    IMMEDIATE = "immediate"
    DEFERRED = "deferred"


# ---------------------------------------------------------------------------
# Extraction Service (Worker Pool + Queue)
# ---------------------------------------------------------------------------


class ExtractionService:
    """Manages the extraction worker pool and queue.

    Spawns one asyncio.Task per concurrency slot across all configured
    endpoints. Each worker pulls record IDs from a shared queue, acquires
    the endpoint's semaphore to respect its concurrency limit, and calls
    _process to handle the extraction lifecycle.
    """

    def __init__(
        self,
        config: AppConfig,
        session_factory,
        thrustcurve_service=None,
        flier_match_service=None,
    ) -> None:
        """Initialise the extraction service.

        Args:
            config: The application configuration (endpoints, mode, date range).
            session_factory: An async_sessionmaker for creating DB sessions.
            thrustcurve_service: Optional ThrustCurveService for motor lookups.
            flier_match_service: Optional FlierMatchService for known flier matching.
        """
        self._config = config
        self._mode = ExtractionMode(config.extraction_mode)
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._session_factory = session_factory
        self._endpoints = config.extraction_endpoints
        self._workers: list[asyncio.Task] = []
        self._thrustcurve = thrustcurve_service
        self._flier_match_service = flier_match_service
        self._auto_accept_threshold = config.auto_accept_threshold
        # One semaphore per endpoint, keyed by URL
        self._endpoint_semaphores: dict[str, asyncio.Semaphore] = {
            ep.url: asyncio.Semaphore(ep.concurrency) for ep in self._endpoints
        }

    @property
    def mode(self) -> ExtractionMode:
        """Return the current extraction mode."""
        return self._mode

    async def start(self) -> None:
        """Start extraction workers. Called during app lifespan startup.

        Spawns one worker Task per concurrency slot per endpoint.
        """
        for ep in self._endpoints:
            sem = self._endpoint_semaphores[ep.url]
            for i in range(ep.concurrency):
                task = asyncio.create_task(
                    self._worker(ep, sem),
                    name=f"extractor-{ep.url}-{i}",
                )
                self._workers.append(task)
        logger.info(
            "Extraction service started: %d workers across %d endpoints",
            len(self._workers),
            len(self._endpoints),
        )

    async def stop(self) -> None:
        """Gracefully stop extraction workers. Called during app lifespan shutdown.

        Waits up to 30 seconds for the queue to drain, then cancels all workers.
        """
        try:
            await asyncio.wait_for(self._queue.join(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning(
                "Extraction queue did not drain within 30s; cancelling workers"
            )

        for worker in self._workers:
            worker.cancel()

        # Wait for all workers to finish cancellation
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        logger.info("Extraction service stopped")

    async def enqueue(self, record_id: int) -> None:
        """Enqueue a record for extraction.

        In IMMEDIATE mode the record ID is placed on the queue immediately.
        In DEFERRED mode this is a no-op — the record stays pending until
        trigger_pending() or a mode switch to IMMEDIATE.
        """
        if self._mode == ExtractionMode.IMMEDIATE:
            await self._queue.put(record_id)

    async def force_enqueue(self, record_id: int) -> None:
        """Enqueue a record for extraction regardless of mode."""
        await self._queue.put(record_id)

    async def set_mode(self, mode: ExtractionMode) -> None:
        """Switch the extraction operating mode.

        If switching from DEFERRED to IMMEDIATE, automatically triggers
        dispatch of all pending records.
        """
        old_mode = self._mode
        self._mode = mode
        logger.info("Extraction mode changed: %s → %s", old_mode.value, mode.value)
        if old_mode == ExtractionMode.DEFERRED and mode == ExtractionMode.IMMEDIATE:
            await self.trigger_pending()

    async def trigger_pending(self) -> int:
        """Enqueue all pending records for extraction regardless of mode.

        Returns:
            The number of records enqueued.
        """
        # Import here to avoid circular imports at module level
        from flight_card_scanner.services import record_service

        async with self._session_factory() as db:
            records = await record_service.get_by_status(db, "pending")
            for record in records:
                await self._queue.put(record.id)
            count = len(records)

        if count > 0:
            logger.info("Triggered %d pending records for extraction", count)
        return count

    async def _worker(
        self, endpoint: EndpointConfig, sem: asyncio.Semaphore
    ) -> None:
        """Infinite worker loop for a single endpoint concurrency slot.

        Pulls record IDs from the queue, acquires the endpoint semaphore,
        processes the record, then releases and marks the task done.
        """
        async with httpx.AsyncClient(
            base_url=endpoint.url, timeout=600.0
        ) as client:
            while True:
                record_id = await self._queue.get()
                try:
                    async with sem:
                        await self._process(record_id, client, endpoint.url)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(
                        "Unexpected error processing record %d on %s: %s",
                        record_id,
                        endpoint.url,
                        exc,
                    )
                finally:
                    self._queue.task_done()

    async def _process(
        self, record_id: int, client: httpx.AsyncClient, endpoint_url: str
    ) -> None:
        """Process a single record: set processing, call Ollama, apply results.

        On success, applies extraction and sets status to 'extracted'.
        On failure (parse error, unavailable endpoint, date resolution error),
        sets status to 'extraction_failed'.
        """
        from flight_card_scanner.services import record_service

        # Fetch record and set status to processing
        async with self._session_factory() as db:
            record = await record_service.get(db, record_id)
            if record is None:
                logger.warning("Record %d not found; skipping", record_id)
                return
            await record_service.set_status(db, record_id, "processing")

        try:
            extracted = await self._call_ollama(client, record.image_path, record_id)
        except OllamaUnavailableError as exc:
            logger.error(
                "Endpoint %s unreachable for record %d: %s — returning to pending",
                endpoint_url,
                record_id,
                exc,
            )
            async with self._session_factory() as db:
                await record_service.set_status(db, record_id, "pending")
            return
        except ExtractionParseError as exc:
            logger.error(
                "Bad JSON from LLM for record %d: %s (raw: %s)",
                record_id,
                exc.message,
                exc.raw_response[:200],
            )
            async with self._session_factory() as db:
                await record_service.set_status(db, record_id, "extraction_failed")
            return

        # Resolve flight date — failure is non-fatal; just leave date as None
        try:
            resolved_date = resolve_flight_date(
                extracted.flight_date_raw, self._config.event_date_range
            )
        except DateResolutionError as exc:
            logger.warning(
                "Date resolution failed for record %d: %s", record_id, exc
            )
            resolved_date = None

        # Apply successful extraction
        async with self._session_factory() as db:
            await record_service.apply_extraction(
                db, record_id, extracted, resolved_date
            )

        # Post-extraction: look up motors via ThrustCurve
        if self._thrustcurve and extracted.motors:
            try:
                async with self._session_factory() as db:
                    record = await record_service.get(db, record_id)
                    if record and record.overflow and record.overflow.get("motors"):
                        import copy
                        motors = copy.deepcopy(record.overflow["motors"])
                        annotated = await self._thrustcurve.lookup_motors(motors)
                        overflow = copy.deepcopy(record.overflow)
                        overflow["motors"] = annotated
                        await record_service.update_fields(
                            db, record_id, {"overflow": overflow}
                        )
            except Exception as exc:
                logger.warning(
                    "ThrustCurve lookup failed for record %d: %s",
                    record_id,
                    exc,
                )

        # Post-extraction: flier verification via known fliers list
        if self._flier_match_service and self._flier_match_service.enabled:
            try:
                async with self._session_factory() as db:
                    record = await record_service.get(db, record_id)
                    if record:
                        # Extract membership info from overflow
                        membership = (record.overflow or {}).get("membership", {})
                        result = await self._flier_match_service.match_flier(
                            flier_name=record.flier_name,
                            club=membership.get("club"),
                            member_number=membership.get("member_number"),
                            cert_level=membership.get("cert_level"),
                        )
                        await self._apply_flier_match(db, record_id, result)
            except Exception as exc:
                logger.warning(
                    "Flier verification failed for record %d: %s",
                    record_id,
                    exc,
                )

    async def _apply_flier_match(
        self,
        db,
        record_id: int,
        result,
    ) -> None:
        """Apply flier match result to the database record.

        Three cases:
        1. Error or no match → store status only
        2. High confidence (> auto_accept_threshold) → auto-accept,
           set flier_verified=True, import roster data
        3. Lower confidence (matched but <= auto_accept_threshold) → flag for
           review, set flier_verified=False, import roster data identically

        Both matched tiers apply the SAME data import: roster name replaces
        flier_name, both NAR/TRA numbers are stored, cert_level is stored,
        and confidence is recorded. The only difference is flier_verified
        and flier_match_status.
        """
        from flight_card_scanner.services import record_service
        from flight_card_scanner.services.flier_match_service import FlierMatchResult

        record = await record_service.get(db, record_id)
        if record is None:
            return

        overflow = dict(record.overflow or {})

        if result.error:
            overflow["flier_match_status"] = "error"
            overflow["flier_match_error"] = str(result.error)
            record.overflow = overflow
            await db.commit()
            return

        if not result.matched:
            overflow["flier_match_status"] = "not_found"
            record.overflow = overflow
            await db.commit()
            return

        # --- Match found: apply roster data (same for both tiers) ---

        # Use the service to extract data using detected column names
        roster_data = self._flier_match_service.extract_roster_data(result.row_data)

        # Apply roster name to the record
        record.flier_name = roster_data["name"] or record.flier_name

        # Store roster membership data in the format the system expects
        # (compatible with MembershipInfo schema and detail template)
        membership = {}
        # Store both club numbers from the roster
        nar_num = roster_data["nar_number"]
        tra_num = roster_data["tra_number"]
        membership["nar_number"] = nar_num
        membership["tra_number"] = tra_num
        # Set the primary club/member_number for the standard fields
        # (what templates and the rest of the system read)
        if nar_num:
            membership["club"] = "NAR"
            membership["member_number"] = nar_num
        elif tra_num:
            membership["club"] = "TRA"
            membership["member_number"] = tra_num
        # Cert level
        if roster_data["cert_level"] is not None:
            membership["cert_level"] = roster_data["cert_level"]
        overflow["membership"] = membership

        # Store confidence
        overflow["flier_match_confidence"] = result.confidence

        # Tier-specific: only flier_verified and status differ
        if result.confidence > self._auto_accept_threshold:
            overflow["flier_match_status"] = "verified"
            record.flier_verified = True
        else:
            overflow["flier_match_status"] = "review"
            record.flier_verified = False

        record.overflow = overflow
        await db.commit()

    async def _call_ollama(
        self, client: httpx.AsyncClient, image_path: str, record_id: int
    ) -> FlightCardExtraction:
        """Submit card image to Ollama and return parsed extraction.

        Reads the image, base64-encodes it, sends to the Ollama /api/chat
        endpoint with structured output format, and parses the response
        into a FlightCardExtraction model.

        Raises:
            OllamaUnavailableError: If the Ollama endpoint returns an HTTP error.
            ExtractionParseError: If the LLM response cannot be validated.
        """
        # Read image, resize to 1600px tall for LLM context efficiency, then base64-encode
        full_path = self._config.image_store_path / image_path
        image_bytes = full_path.read_bytes()

        from io import BytesIO
        from PIL import Image

        img = Image.open(BytesIO(image_bytes))
        target_height = 1600
        if img.height > target_height:
            scale = target_height / img.height
            new_width = int(img.width * scale)
            img = img.resize((new_width, target_height), Image.LANCZOS)
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=90)
            resized_bytes = buf.getvalue()
        else:
            resized_bytes = image_bytes

        b64_image = base64.b64encode(resized_bytes).decode("ascii")

        # Build the Ollama /api/chat payload
        payload = {
            "model": "qwen3-vl",
            "messages": [
                {
                    "role": "user",
                    "content": EXTRACTION_PROMPT.format(
                        event_start=self._config.event_date_range.start.strftime("%B %-d, %Y"),
                        event_end=self._config.event_date_range.end.strftime("%B %-d, %Y"),
                    ),
                    "images": [b64_image],
                }
            ],
            "format": _simplify_schema(FlightCardExtraction.model_json_schema()),
            "stream": False,
            "options": {"temperature": 0, "num_ctx": 32768, "num_predict": 8192},
            "think": True,
        }

        # Save the request payload as a sidecar .request file (without the base64 image)
        request_filename = Path(image_path).stem + ".request"
        request_path = self._config.image_store_path / request_filename
        try:
            # Replace the large base64 image with a placeholder for readability
            request_dump = json.loads(json.dumps(payload))
            for msg in request_dump.get("messages", []):
                if "images" in msg:
                    msg["images"] = [f"<base64 image: {len(b64_image)} chars>"]
            request_path.write_text(json.dumps(request_dump, indent=2, ensure_ascii=False))
        except OSError as exc:
            logger.warning("Failed to write request file %s: %s", request_path, exc)

        # Send request to Ollama
        try:
            logger.info(
                "sending record %d to Ollama at %s",
                record_id,
                client.base_url,
            )
            import time as _time
            _t0 = _time.monotonic()
            response = await client.post("/api/chat", json=payload)
            response.raise_for_status()
            _elapsed = _time.monotonic() - _t0
            logger.info(
                "Ollama at %s responded for record %d in %.1fs",
                client.base_url,
                record_id,
                _elapsed,
            )
        except httpx.HTTPStatusError as exc:
            raise OllamaUnavailableError(
                f"Ollama returned HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise OllamaUnavailableError(
                f"Ollama request failed: {exc}"
            ) from exc

        # Dump full Ollama response to a .json file alongside the image
        data = response.json()
        json_filename = Path(image_path).stem + ".json"
        json_path = self._config.image_store_path / json_filename
        try:
            json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        except OSError as exc:
            logger.warning("Failed to write debug JSON %s: %s", json_path, exc)

        # Parse the response
        raw_content = data["message"]["content"]

        if not raw_content or not raw_content.strip():
            done_reason = data.get("done_reason", "unknown")
            raise ExtractionParseError(
                message=f"LLM returned empty content (done_reason: {done_reason})",
                raw_response=raw_content or "(empty)",
            )

        # Strip any <think>...</think> block that the model may have embedded in content
        # Also handle case where <think> is present but </think> is missing
        # (model started thinking but didn't close the tag before JSON) OR
        # </think> is present but <think> is missing (trailing thinking is
        # present, but not the start of it.)
        cleaned_content = raw_content.strip()
        if "think>" in cleaned_content:
            # Remove everything up to </think>
            logger.warning("found junk, trimming content")
            cleaned_content = re.sub(
                r".*?</think>", "", cleaned_content, flags=re.DOTALL
            ).strip()


        if not cleaned_content:
            raise ExtractionParseError(
                message="LLM content was only a think block with no JSON",
                raw_response=raw_content,
            )

        # Pre-process: fix measurements where model merged value+unit into one field
        parsed_json = json.loads(cleaned_content)

        if parsed_json is not None:
            measurements = parsed_json.get("measurements")
            if measurements and isinstance(measurements, dict):
                for dim in ("diameter", "length", "weight"):
                    val = measurements.get(dim)
                    unit_key = f"{dim}_unit"
                    if isinstance(val, str):
                        # Try to split trailing unit from number, e.g. "2in" -> 2, "in"
                        match = re.match(r"^([0-9]*\.?[0-9]+)\s*([a-zA-Z\"\']+)$", val)
                        if match:
                            measurements[dim] = float(match.group(1))
                            if not measurements.get(unit_key):
                                measurements[unit_key] = match.group(2)
                        else:
                            # Try to parse as plain number
                            try:
                                measurements[dim] = float(val)
                            except ValueError:
                                pass
            cleaned_content = json.dumps(parsed_json)

        return FlightCardExtraction.model_validate_json(cleaned_content)

# Day-of-week name mapping (full and abbreviated, lowercase) to Python weekday int
_DAY_NAMES: dict[str, int] = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

# Date formats to attempt, in priority order.
# The %m/%d format is handled separately to avoid Python 3.15 deprecation.
_DATE_FORMATS_WITH_YEAR = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y")


def resolve_flight_date(
    raw_value: str | None,
    date_range: DateRange,
) -> date | None:
    """Resolve a raw date string from the LLM to a calendar date.

    Cases handled:
    1. None / empty  → return None  (no date written on card)
    2. Day-of-week name (e.g. "Saturday", "Sat") →
         find the unique day within event_date_range that matches;
         if no match → raise DateResolutionError
    3. Numeric / ISO date string (e.g. "2025-07-19", "7/19/2025",
       "7/19/25", "7/19") →
         parse to a date; validate it falls within event_date_range;
         if out of range → raise DateResolutionError
    4. Unrecognised format → raise DateResolutionError

    Returns the resolved date, or None if raw_value is None/empty.
    Raises DateResolutionError if the value cannot be resolved to a date
    within the event date range.
    """
    if raw_value is None:
        return None

    stripped = raw_value.strip()
    if not stripped:
        return None

    normalized = stripped.lower()

    # --- Day-of-week resolution ---
    if normalized in _DAY_NAMES:
        target_weekday = _DAY_NAMES[normalized]
        current = date_range.start
        while current <= date_range.end:
            if current.weekday() == target_weekday:
                return current
            current += timedelta(days=1)
        raise DateResolutionError(
            f"Day-of-week '{raw_value}' does not occur within the event date range "
            f"({date_range.start} to {date_range.end})"
        )

    # --- Contradictory day+date combination ---
    # Handle cases like "Friday 7/13" or "Fri 2025-07-13" where the day name
    # and the numeric date disagree. The day-of-week is trusted over the number.
    # Pattern: optional day name prefix followed by a numeric date portion.
    contradiction_match = re.match(
        r"^([a-zA-Z]+)\s+(.+)$", stripped
    )
    if contradiction_match:
        day_part = contradiction_match.group(1).lower()
        date_part = contradiction_match.group(2).strip()
        if day_part in _DAY_NAMES:
            # We have a day name + something else — resolve by day name
            target_weekday = _DAY_NAMES[day_part]
            current = date_range.start
            while current <= date_range.end:
                if current.weekday() == target_weekday:
                    return current
                current += timedelta(days=1)
            raise DateResolutionError(
                f"Day-of-week '{contradiction_match.group(1)}' (from '{raw_value}') "
                f"does not occur within the event date range "
                f"({date_range.start} to {date_range.end})"
            )

    # --- Numeric / ISO date parsing ---
    for fmt in _DATE_FORMATS_WITH_YEAR:
        try:
            parsed = datetime.strptime(stripped, fmt).date()
        except ValueError:
            continue

        # Validate within range
        if date_range.start <= parsed <= date_range.end:
            return parsed
        raise DateResolutionError(
            f"Date '{raw_value}' (parsed as {parsed}) falls outside the event date range "
            f"({date_range.start} to {date_range.end})"
        )

    # Try M/D format (no year) — manually parse to avoid Python 3.15 deprecation
    md_match = re.match(r"^(\d{1,2})/(\d{1,2})$", stripped)
    if md_match:
        try:
            month = int(md_match.group(1))
            day = int(md_match.group(2))
            parsed = date(date_range.start.year, month, day)
        except ValueError:
            pass
        else:
            if date_range.start <= parsed <= date_range.end:
                return parsed
            raise DateResolutionError(
                f"Date '{raw_value}' (parsed as {parsed}) falls outside the event date range "
                f"({date_range.start} to {date_range.end})"
            )

    raise DateResolutionError(
        f"Cannot resolve '{raw_value}' to a valid date"
    )
