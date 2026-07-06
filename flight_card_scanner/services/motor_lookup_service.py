from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Path to the thrustcurve-db JSON relative to the package directory
_PACKAGE_DIR = Path(__file__).resolve().parent.parent
_THRUSTCURVE_DB_PATH = (
    _PACKAGE_DIR / "static" / "js" / "node_modules" / "thrustcurve-db" / "thrustcurve-db.json"
)

# Manufacturer alias table: lowercased alias → canonical abbreviation
_MANUFACTURER_ALIASES: dict[str, str] = {
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


class MotorLookupService:
    """In-memory motor database loaded from thrustcurve-db npm package."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or _THRUSTCURVE_DB_PATH
        # Primary storage: list of all motor dicts
        self._motors: list[dict[str, Any]] = []
        # Indexes for fast lookup
        self._by_common_name: dict[str, list[dict[str, Any]]] = {}
        self._by_motor_id: dict[str, dict[str, Any]] = {}

    async def startup(self) -> None:
        """Load and index the motor database. Raises on failure."""
        self._load_database()

    def _load_database(self) -> None:
        """Read JSON, parse, and build indexes."""
        if not self._db_path.exists():
            raise RuntimeError(
                f"thrustcurve-db JSON not found at {self._db_path}. "
                "Run 'pnpm install' in the project root."
            )
        try:
            raw = self._db_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Failed to load thrustcurve-db: {exc}"
            ) from exc

        # data is expected to be a list of motor objects
        if isinstance(data, list):
            self._motors = data
        elif isinstance(data, dict):
            # Some versions export as {"motors": [...]}
            self._motors = data.get("motors", data.get("data", []))
        else:
            raise RuntimeError("Unexpected thrustcurve-db format")

        self._build_indexes()
        logger.info(
            "Motor database loaded: %d motors indexed", len(self._motors)
        )

    @property
    def _metadata(self) -> dict[str, Any]:
        """Derive metadata (impulse classes and manufacturers) from loaded motors."""
        impulse_classes: set[str] = set()
        manufacturers: dict[str, str] = {}  # abbrev -> abbrev

        for motor in self._motors:
            # Extract impulse class letter from commonName (first char)
            cn = motor.get("commonName", "")
            if cn:
                letter = cn.strip()[0].upper()
                if letter.isalpha():
                    impulse_classes.add(letter)

            # Collect unique manufacturers
            abbrev = motor.get("manufacturerAbbrev", "")
            if abbrev and abbrev not in manufacturers:
                manufacturers[abbrev] = abbrev

        # Sort impulse classes in standard order
        standard_order = ["\u00bcA", "\u00bdA"] + list("ABCDEFGHIJKLMNOP")
        sorted_classes = sorted(
            impulse_classes, key=lambda c: standard_order.index(c) if c in standard_order else 99
        )

        sorted_manufacturers = [
            {"abbrev": a} for a in sorted(manufacturers.keys())
        ]

        return {
            "impulseClasses": sorted_classes,
            "manufacturers": sorted_manufacturers,
        }

    def _build_indexes(self) -> None:
        """Build lookup indexes from the motor list."""
        for motor in self._motors:
            # Index by commonName (case-insensitive key)
            cn = motor.get("commonName", "")
            if cn:
                key = cn.strip().upper()
                self._by_common_name.setdefault(key, []).append(motor)

            # Index by motorId
            mid = motor.get("motorId")
            if mid:
                self._by_motor_id[str(mid)] = motor

    def resolve_manufacturer(self, extracted_mfr: str | None) -> str | None:
        """Resolve an extracted manufacturer string to canonical abbreviation."""
        if not extracted_mfr:
            return None

        mfr_lower = extracted_mfr.strip().lower()

        # Check alias table
        if mfr_lower in _MANUFACTURER_ALIASES:
            return _MANUFACTURER_ALIASES[mfr_lower]

        # Direct match against database manufacturers
        seen: set[str] = set()
        for motor in self._motors:
            abbrev = motor.get("manufacturerAbbrev", "")
            if abbrev and abbrev not in seen:
                seen.add(abbrev)
                if mfr_lower == abbrev.lower():
                    return abbrev

        # Substring match
        for abbrev in seen:
            if mfr_lower in abbrev.lower() or abbrev.lower() in mfr_lower:
                return abbrev

        return None

    def search_motors(
        self, common_name: str, manufacturer: str | None = None
    ) -> list[dict[str, Any]]:
        """Search for motors by common name and optional manufacturer."""
        key = common_name.strip().upper()
        candidates = self._by_common_name.get(key, [])

        if manufacturer and candidates:
            resolved_mfr = self.resolve_manufacturer(manufacturer)
            if resolved_mfr:
                candidates = [
                    m for m in candidates
                    if m.get("manufacturerAbbrev", "").lower() == resolved_mfr.lower()
                ]

        return candidates

    def get_motor_by_id(self, motor_id: str) -> dict[str, Any] | None:
        """Get a single motor by its ID."""
        return self._by_motor_id.get(str(motor_id))

    async def lookup_motors(
        self, motors: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Look up each motor and annotate with results.

        For each motor dict, adds one of:
          - thrustcurve_id: str (unique match)
          - thrustcurve_candidates: list (multiple matches)
          - thrustcurve_error: str (no match or insufficient data)
        """
        for motor in motors:
            if motor.get("thrustcurve_id"):
                continue

            letter = motor.get("letter", "")
            number = motor.get("number", "")

            if not letter:
                motor["thrustcurve_error"] = "Insufficient motor data to search"
                motor.pop("thrustcurve_id", None)
                motor.pop("thrustcurve_candidates", None)
                continue

            common_name = f"{letter}{number}" if number else letter
            manufacturer = motor.get("manufacturer")
            results = self.search_motors(common_name, manufacturer)

            if len(results) == 1:
                match = results[0]
                motor["thrustcurve_id"] = str(match.get("motorId"))
                motor["thrustcurve_data"] = {
                    "commonName": match.get("commonName"),
                    "manufacturerAbbrev": match.get("manufacturerAbbrev"),
                    "designation": match.get("designation"),
                    "propInfo": match.get("propInfo"),
                    "totImpulseNs": match.get("totImpulseNs"),
                    "avgThrustN": match.get("avgThrustN"),
                    "diameter": match.get("diameter"),
                    "impulseClass": match.get("impulseClass"),
                    "source_url": match.get("source_url"),
                }
                motor.pop("thrustcurve_candidates", None)
                motor.pop("thrustcurve_error", None)
            elif len(results) > 1:
                motor["thrustcurve_candidates"] = [
                    {
                        "motorId": str(r.get("motorId")),
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
                ]
                motor.pop("thrustcurve_id", None)
                motor.pop("thrustcurve_error", None)
            else:
                motor["thrustcurve_error"] = f"No matches found for '{common_name}'"
                motor.pop("thrustcurve_id", None)
                motor.pop("thrustcurve_candidates", None)

        return motors

    async def search_motor(self, motor_data: dict[str, Any]) -> dict[str, Any]:
        """Search for a single motor — adapter for admin router compatibility.

        Args:
            motor_data: Dict with 'letter', 'number', 'manufacturer', 'suffix' keys

        Returns:
            Dict with 'motorId' (str or None), 'candidates' (list), 'error' (str or None), 'query' (str)
        """
        letter = motor_data.get("letter", "")
        number = motor_data.get("number", "")
        manufacturer = motor_data.get("manufacturer")

        if not letter:
            return {"motorId": None, "candidates": [], "error": "No motor letter provided", "query": ""}

        common_name = f"{letter}{number}" if number else letter
        results = self.search_motors(common_name, manufacturer)

        if len(results) == 1:
            return {
                "motorId": str(results[0].get("motorId")),
                "candidates": [],
                "error": None,
                "query": common_name,
            }
        elif len(results) > 1:
            candidates = [
                {
                    "motorId": str(r.get("motorId")),
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
            ]
            return {
                "motorId": None,
                "candidates": candidates,
                "error": None,
                "query": common_name,
            }
        else:
            return {
                "motorId": None,
                "candidates": [],
                "error": f"No matches found for '{common_name}'",
                "query": common_name,
            }

    async def get_motor(self, motor_id: str) -> dict[str, Any] | None:
        """Get motor data by ID — adapter for admin router compatibility."""
        motor = self.get_motor_by_id(motor_id)
        if motor is None:
            return None
        return {
            "motorId": str(motor.get("motorId")),
            "commonName": motor.get("commonName"),
            "manufacturerAbbrev": motor.get("manufacturerAbbrev"),
            "designation": motor.get("designation"),
            "propInfo": motor.get("propInfo"),
            "totImpulseNs": motor.get("totImpulseNs"),
            "avgThrustN": motor.get("avgThrustN"),
            "diameter": motor.get("diameter"),
            "impulseClass": motor.get("impulseClass"),
            "availability": motor.get("availability"),
        }

    async def enrich_motors_for_display(
        self, motors: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Enrich motor dicts with full metadata for display.

        For each motor with a thrustcurve_id, adds thrustcurve_data dict.
        """
        enriched = []
        for motor in motors:
            motor_copy = dict(motor)
            tc_id = motor_copy.get("thrustcurve_id")
            if tc_id:
                motor_data = self.get_motor_by_id(tc_id)
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
                else:
                    motor_copy["thrustcurve_data"] = None
            enriched.append(motor_copy)
        return enriched
