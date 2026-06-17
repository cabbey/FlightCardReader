"""ThrustCurve.org API integration service.

Provides:
- Metadata fetching with HTTP conditional (If-Modified-Since) caching
- Motor search by extracted motor parameters
- Per-motor data caching on the filesystem
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TC_BASE = "https://www.thrustcurve.org/api/v1"


class ThrustCurveService:
    """Manages communication with the ThrustCurve.org API and local caching."""

    def __init__(self, cache_dir: Path) -> None:
        """Initialise the service.

        Args:
            cache_dir: Directory for cached metadata and motor data files.
        """
        self._cache_dir = cache_dir
        self._motors_dir = cache_dir / "motors"
        self._metadata_path = cache_dir / "metadata.json"
        self._metadata_headers_path = cache_dir / "metadata_headers.json"
        self._metadata: dict[str, Any] | None = None
        # Track which motor IDs have been freshness-checked this session
        self._session_checked: set[str] = set()

        # Ensure cache directories exist
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._motors_dir.mkdir(parents=True, exist_ok=True)

    async def startup(self) -> None:
        """Fetch or refresh metadata at application startup."""
        await self._refresh_metadata()

    async def _refresh_metadata(self) -> None:
        """Fetch metadata from ThrustCurve, using If-Modified-Since if cached."""
        headers: dict[str, str] = {}

        # Load cached headers for conditional request
        if self._metadata_headers_path.exists():
            try:
                cached_headers = json.loads(self._metadata_headers_path.read_text())
                last_modified = cached_headers.get("last-modified")
                if last_modified:
                    headers["If-Modified-Since"] = last_modified
            except (json.JSONDecodeError, OSError):
                pass

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{_TC_BASE}/metadata.json", headers=headers
                )

                if resp.status_code == 304:
                    logger.info("ThrustCurve metadata not modified, using cache")
                    self._load_cached_metadata()
                    return

                resp.raise_for_status()
                data = resp.json()

                # Save to cache
                self._metadata_path.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False)
                )

                # Save response headers for future conditional requests
                resp_headers = {
                    "last-modified": resp.headers.get("last-modified", ""),
                    "etag": resp.headers.get("etag", ""),
                    "date": resp.headers.get("date", ""),
                }
                self._metadata_headers_path.write_text(
                    json.dumps(resp_headers)
                )

                self._metadata = data
                logger.info(
                    "ThrustCurve metadata refreshed: %d manufacturers, %d impulse classes",
                    len(data.get("manufacturers", [])),
                    len(data.get("impulseClasses", [])),
                )

        except httpx.HTTPError as exc:
            logger.warning("Failed to fetch ThrustCurve metadata: %s", exc)
            self._load_cached_metadata()

    def _load_cached_metadata(self) -> None:
        """Load metadata from the local cache file."""
        if self._metadata_path.exists():
            try:
                self._metadata = json.loads(self._metadata_path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("Failed to load cached metadata: %s", exc)
                self._metadata = None
        else:
            self._metadata = None

    def _resolve_manufacturer(self, extracted_mfr: str | None) -> str | None:
        """Match an extracted manufacturer string to a ThrustCurve abbreviation.

        Uses common nickname/abbreviation mappings and the metadata manufacturers
        list for case-insensitive matching.
        """
        if not extracted_mfr or not self._metadata:
            return None

        mfr_lower = extracted_mfr.strip().lower()
        manufacturers = self._metadata.get("manufacturers", [])

        # Common abbreviation and nickname mappings to TC abbreviations
        _ALIASES: dict[str, str] = {
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

        # Check aliases first
        if mfr_lower in _ALIASES:
            alias_target = _ALIASES[mfr_lower]
            for m in manufacturers:
                if m.get("abbrev", "") == alias_target:
                    return m["abbrev"]
                if m.get("name", "").lower() == alias_target.lower():
                    return m["abbrev"]
            # If exact match not found in metadata, return the alias target anyway
            # (TC API may still accept it)
            return alias_target

        # Direct case-insensitive match on abbreviation or name
        for m in manufacturers:
            abbrev = m.get("abbrev", "")
            name = m.get("name", "")
            if mfr_lower == abbrev.lower() or mfr_lower == name.lower():
                return abbrev

        # Substring match (e.g. "Aero" matching "AeroTech")
        for m in manufacturers:
            abbrev = m.get("abbrev", "")
            name = m.get("name", "")
            if mfr_lower in abbrev.lower() or mfr_lower in name.lower():
                return abbrev
            if abbrev.lower() in mfr_lower or name.lower() in mfr_lower:
                return abbrev

        return None

    def build_search_query(self, motor: dict[str, Any]) -> dict[str, Any]:
        """Build a ThrustCurve search request from extracted motor data.

        Args:
            motor: A motor dict from the overflow (letter, number, manufacturer, etc.)

        Returns:
            A dict suitable for POST to /search.json
        """
        query: dict[str, Any] = {}

        letter = motor.get("letter", "")
        number = motor.get("number", "")

        if letter and number:
            # commonName is letter + number, e.g. "H128"
            query["commonName"] = f"{letter}{number}"

        if letter:
            query["impulseClass"] = letter.upper()

        # Try to resolve manufacturer
        mfr = self._resolve_manufacturer(motor.get("manufacturer"))
        if mfr:
            query["manufacturer"] = mfr

        # Limit results
        query["maxResults"] = 20

        return query

    async def search_motor(self, motor: dict[str, Any]) -> dict[str, Any]:
        """Search ThrustCurve for a motor matching the extracted data.

        Args:
            motor: A motor dict from the overflow.

        Returns:
            A dict with keys:
              - "motorId": str if uniquely identified, else None
              - "candidates": list of candidate motor dicts if multiple matches
              - "error": str if there was an error
              - "query": the search query that was sent
        """
        query = self.build_search_query(motor)

        if not query.get("commonName") and not query.get("impulseClass"):
            return {
                "motorId": None,
                "candidates": [],
                "error": "Insufficient motor data to search",
                "query": query,
            }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{_TC_BASE}/search.json", json=query
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            error_msg = f"ThrustCurve search failed: {exc}"
            logger.warning(error_msg)
            return {
                "motorId": None,
                "candidates": [],
                "error": error_msg,
                "query": query,
            }

        results = data.get("results", [])
        matches = data.get("matches", 0)

        if matches == 1 and len(results) == 1:
            motor_id = results[0].get("motorId")
            # Cache the motor data
            await self._cache_motor_data(motor_id, results[0])
            return {
                "motorId": motor_id,
                "candidates": [],
                "error": None,
                "query": query,
            }

        if matches == 0 or not results:
            # Check for errors in criteria
            criteria = data.get("criteria", [])
            errors = [c.get("error") for c in criteria if c.get("error")]
            error_msg = "; ".join(errors) if errors else "No matches found"
            logger.info(
                "ThrustCurve search: no unique match. Query=%s, Error=%s",
                json.dumps(query),
                error_msg,
            )
            return {
                "motorId": None,
                "candidates": [],
                "error": error_msg,
                "query": query,
            }

        # Multiple matches
        candidate_ids = [r.get("motorId") for r in results if r.get("motorId")]
        logger.info(
            "ThrustCurve search: %d candidates. Query=%s, IDs=%s",
            len(candidate_ids),
            json.dumps(query),
            candidate_ids,
        )

        # Cache all candidate motor data
        for r in results:
            mid = r.get("motorId")
            if mid:
                await self._cache_motor_data(mid, r)

        return {
            "motorId": None,
            "candidates": [
                {
                    "motorId": r.get("motorId"),
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
            ],
            "error": None,
            "query": query,
        }

    async def _cache_motor_data(self, motor_id: str, data: dict[str, Any]) -> None:
        """Save motor search result data to the filesystem cache."""
        motor_path = self._motors_dir / f"{motor_id}.json"
        try:
            motor_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        except OSError as exc:
            logger.warning("Failed to cache motor %s: %s", motor_id, exc)

    async def get_motor(self, motor_id: str) -> dict[str, Any] | None:
        """Retrieve motor data by ID, using cache with session freshness check.

        On the first access per server session, performs an If-Modified-Since
        check against ThrustCurve. Subsequent accesses use the cache directly.
        """
        motor_path = self._motors_dir / f"{motor_id}.json"

        # If already checked this session, just return cache
        if motor_id in self._session_checked:
            if motor_path.exists():
                try:
                    return json.loads(motor_path.read_text())
                except (json.JSONDecodeError, OSError):
                    pass

        # First access this session — do a freshness check
        self._session_checked.add(motor_id)

        if motor_path.exists():
            try:
                cached_data = json.loads(motor_path.read_text())
            except (json.JSONDecodeError, OSError):
                cached_data = None

            if cached_data:
                # Use updatedOn from cached data for If-Modified-Since
                updated_on = cached_data.get("updatedOn")
                headers: dict[str, str] = {}
                if updated_on:
                    try:
                        dt = datetime.fromisoformat(updated_on).replace(
                            tzinfo=timezone.utc
                        )
                        headers["If-Modified-Since"] = format_datetime(
                            dt, usegmt=True
                        )
                    except (ValueError, TypeError):
                        pass

                # Search for the motor by ID to check freshness
                try:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        resp = await client.post(
                            f"{_TC_BASE}/search.json",
                            json={"id": motor_id, "maxResults": 1},
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            results = data.get("results", [])
                            if results:
                                fresh = results[0]
                                await self._cache_motor_data(motor_id, fresh)
                                return fresh
                except httpx.HTTPError:
                    pass  # Fall back to cached data

                return cached_data

        # No cache — fetch fresh
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{_TC_BASE}/search.json",
                    json={"id": motor_id, "maxResults": 1},
                )
                resp.raise_for_status()
                data = resp.json()
                results = data.get("results", [])
                if results:
                    await self._cache_motor_data(motor_id, results[0])
                    return results[0]
        except httpx.HTTPError as exc:
            logger.warning("Failed to fetch motor %s: %s", motor_id, exc)

        return None

    async def lookup_motors(
        self, motors: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Look up each motor in a list via ThrustCurve and annotate with results.

        Modifies motors in place, adding:
          - "thrustcurve_id": str if uniquely identified
          - "thrustcurve_candidates": list if multiple matches
          - "thrustcurve_error": str if error occurred

        Also logs non-unique results to console.

        Args:
            motors: List of motor dicts from overflow.

        Returns:
            The annotated motor list.
        """
        for motor in motors:
            # Skip if already resolved
            if motor.get("thrustcurve_id"):
                continue

            result = await self.search_motor(motor)

            if result["motorId"]:
                motor["thrustcurve_id"] = result["motorId"]
                motor.pop("thrustcurve_candidates", None)
                motor.pop("thrustcurve_error", None)
                motor.pop("thrustcurve_data", None)
                logger.info(
                    "Motor %s%s uniquely identified: %s",
                    motor.get("letter", "?"),
                    motor.get("number", "?"),
                    result["motorId"],
                )
            elif result["candidates"]:
                motor["thrustcurve_candidates"] = result["candidates"]
                motor.pop("thrustcurve_id", None)
                motor.pop("thrustcurve_error", None)
                logger.info(
                    "Motor %s%s has %d candidates. Query: %s IDs: %s",
                    motor.get("letter", "?"),
                    motor.get("number", "?"),
                    len(result["candidates"]),
                    json.dumps(result["query"]),
                    [c["motorId"] for c in result["candidates"]],
                )
            else:
                motor["thrustcurve_error"] = result.get("error", "Unknown error")
                motor.pop("thrustcurve_id", None)
                motor.pop("thrustcurve_candidates", None)
                logger.info(
                    "Motor %s%s lookup failed. Query: %s Error: %s",
                    motor.get("letter", "?"),
                    motor.get("number", "?"),
                    json.dumps(result["query"]),
                    result.get("error"),
                )

        return motors

    async def enrich_motors_for_display(
        self, motors: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Enrich motor dicts with cached ThrustCurve data for template display.

        For each motor with a thrustcurve_id, reads the cached motor data
        and adds a 'thrustcurve_data' dict for rendering. This data is NOT
        persisted to the database — it's computed at read time from the cache.

        Args:
            motors: List of motor dicts from overflow.

        Returns:
            A new list of motor dicts with thrustcurve_data populated.
        """
        enriched = []
        for motor in motors:
            motor_copy = dict(motor)
            tc_id = motor_copy.get("thrustcurve_id")
            if tc_id:
                motor_data = await self.get_motor(tc_id)
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
                        "source_url": motor_data.get("source_url"),
                    }
            enriched.append(motor_copy)
        return enriched

    def resolve_manufacturer_for_display(self, extracted_mfr: str | None) -> str | None:
        """Public wrapper around _resolve_manufacturer for template use."""
        return self._resolve_manufacturer(extracted_mfr)
