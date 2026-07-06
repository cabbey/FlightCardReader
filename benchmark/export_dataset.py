#!/usr/bin/env python3
"""Export human-verified flight card records into a benchmark dataset.

Reads the project's SQLite database, finds records marked as human_verified,
and exports:
  - The original card image (copied to the dataset directory)
  - A JSON ground truth file containing the verified field values

Usage:
    # Export all verified records
    python -m benchmark.export_dataset --db path/to/flight_cards.db \
        --image-dir path/to/images --output /path/to/dataset

    # Export specific record IDs only
    python -m benchmark.export_dataset --db path/to/flight_cards.db \
        --image-dir path/to/images --output /path/to/dataset \
        --records 1 5 12 47

The output directory will contain:
    dataset/
        manifest.json          # List of all samples with metadata
        images/                # Copies of the original card images
            <record_id>.jpg
        ground_truth/          # Verified extraction results
            <record_id>.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path


def _load_verified_records(
    db_path: Path,
    record_ids: list[int] | None = None,
) -> list[dict]:
    """Query the database for human-verified records with extracted status.

    Args:
        db_path: Path to the SQLite database file.
        record_ids: If provided, only export these specific record IDs.
            Records must still be human_verified and extracted to be included.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    base_query = """
        SELECT
            id, image_path, flight_date, flier_name,
            total_impulse_value, total_impulse_unit,
            flag_heads_up, flag_first_flight, flag_complex,
            rack, pad, fso_rso_initials,
            evaluation_outcome, evaluation_comments,
            recovery_plan, overflow
        FROM flight_records
        WHERE human_verified = 1 AND extraction_status = 'extracted'
    """

    if record_ids:
        placeholders = ",".join("?" for _ in record_ids)
        query = f"{base_query} AND id IN ({placeholders})"
        cursor.execute(query, record_ids)
    else:
        cursor.execute(base_query)

    records = []
    for row in cursor.fetchall():
        record = dict(row)
        # Parse overflow JSON
        if record["overflow"]:
            record["overflow"] = json.loads(record["overflow"])
        else:
            record["overflow"] = {}
        records.append(record)

    conn.close()
    return records


def _build_ground_truth(record: dict) -> dict:
    """Build a ground truth dict from a verified record.

    This mirrors the FlightCardExtraction schema so that benchmark comparison
    can be done field-by-field against model output.
    """
    overflow = record.get("overflow") or {}

    ground_truth = {
        # Dedicated columns
        "flight_date": record["flight_date"],
        "flier_name": record["flier_name"],
        "total_impulse_value": record["total_impulse_value"],
        "total_impulse_unit": record["total_impulse_unit"],
        "flag_heads_up": bool(record["flag_heads_up"]) if record["flag_heads_up"] is not None else None,
        "flag_first_flight": bool(record["flag_first_flight"]) if record["flag_first_flight"] is not None else None,
        "flag_complex": bool(record["flag_complex"]) if record["flag_complex"] is not None else None,
        "rack": record["rack"],
        "pad": record["pad"],
        "fso_rso_initials": record["fso_rso_initials"],
        "evaluation_outcome": record["evaluation_outcome"],
        "evaluation_comments": record["evaluation_comments"],
        "recovery_plan": record["recovery_plan"],
        # Overflow fields
        "membership": overflow.get("membership"),
        "rocket_name": overflow.get("rocket_name"),
        "rocket_manufacturer": overflow.get("rocket_manufacturer"),
        "rocket_colors": overflow.get("rocket_colors"),
        "measurements": overflow.get("rocket_measurements"),
        "motors": overflow.get("motors"),
        "notes": overflow.get("notes"),
    }

    return ground_truth


def _load_event_date_range(db_path: Path) -> tuple[str | None, str | None]:
    """Load the event date range from exported flight_date values.

    Determines the date range by finding the min and max flight_date values
    among verified records. Returns (start, end) as ISO date strings, or
    (None, None) if no dates are available.
    """
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute("""
        SELECT MIN(flight_date), MAX(flight_date)
        FROM flight_records
        WHERE human_verified = 1 AND extraction_status = 'extracted'
            AND flight_date IS NOT NULL
    """)
    row = cursor.fetchone()
    conn.close()
    if row and row[0] and row[1]:
        return row[0], row[1]
    return None, None


def _warn_if_inside_repo(output_dir: Path) -> None:
    """Warn the user if the output path appears to be inside a git repository tree."""
    check = output_dir.resolve()
    while check != check.parent:
        if (check / ".git").exists():
            print(
                f"  WARNING: Output path is inside a git repository ({check}).\n"
                f"  Consider writing benchmark data outside the source tree to avoid "
                f"accidentally committing large files.",
                file=sys.stderr,
            )
            return
        check = check.parent


def export_dataset(
    db_path: Path,
    image_dir: Path,
    output_dir: Path,
    record_ids: list[int] | None = None,
) -> int:
    """Export verified records to a benchmark dataset.

    Args:
        db_path: Path to the SQLite database file.
        image_dir: Path to the image store directory (where image_path is relative to).
        output_dir: Directory to write the dataset into.
        record_ids: If provided, only export these specific record IDs.

    Returns:
        Number of records exported.
    """
    _warn_if_inside_repo(output_dir)

    records = _load_verified_records(db_path, record_ids)

    if not records:
        print("No human-verified records found in the database.", file=sys.stderr)
        return 0

    # Create output structure
    images_out = output_dir / "images"
    gt_out = output_dir / "ground_truth"
    images_out.mkdir(parents=True, exist_ok=True)
    gt_out.mkdir(parents=True, exist_ok=True)

    samples = []
    exported = 0

    for record in records:
        record_id = record["id"]
        image_path = image_dir / record["image_path"]

        if not image_path.exists():
            print(
                f"  WARNING: Image not found for record {record_id}: {image_path}",
                file=sys.stderr,
            )
            continue

        # Copy image
        ext = image_path.suffix or ".jpg"
        dest_image = images_out / f"{record_id}{ext}"
        shutil.copy2(image_path, dest_image)

        # Write ground truth
        ground_truth = _build_ground_truth(record)
        gt_file = gt_out / f"{record_id}.json"
        gt_file.write_text(json.dumps(ground_truth, indent=2, default=str))

        samples.append({
            "record_id": record_id,
            "image_file": f"images/{record_id}{ext}",
            "ground_truth_file": f"ground_truth/{record_id}.json",
            "flier_name": record["flier_name"],
            "flight_date": record["flight_date"],
        })
        exported += 1

    # Determine event date range from the data
    event_start, event_end = _load_event_date_range(db_path)

    # Build manifest with metadata at the top level
    manifest = {
        "event_date_range": {
            "start": event_start,
            "end": event_end,
        },
        "samples": samples,
    }

    # If a manifest already exists (incremental export), merge date ranges
    manifest_file = output_dir / "manifest.json"
    if manifest_file.exists():
        existing = json.loads(manifest_file.read_text())
        existing_range = existing.get("event_date_range", {})
        if existing_range.get("start") and event_start:
            manifest["event_date_range"]["start"] = min(
                existing_range["start"], event_start
            )
        if existing_range.get("end") and event_end:
            manifest["event_date_range"]["end"] = max(
                existing_range["end"], event_end
            )

    manifest_file.write_text(json.dumps(manifest, indent=2, default=str))

    print(f"Exported {exported} verified records to {output_dir}")
    if event_start and event_end:
        print(f"Event date range: {event_start} to {event_end}")
    return exported


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export human-verified records to a benchmark dataset."
    )
    parser.add_argument(
        "--db",
        type=Path,
        required=True,
        help="Path to the flight_cards.db SQLite database",
    )
    parser.add_argument(
        "--image-dir",
        type=Path,
        required=True,
        help="Path to the image store directory (parent of image_path values)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output directory where the benchmark dataset will be written",
    )
    parser.add_argument(
        "--records",
        type=int,
        nargs="+",
        default=None,
        help="Specific record IDs to export (default: all human-verified records)",
    )

    args = parser.parse_args()

    if not args.db.exists():
        print(f"Error: Database not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    if not args.image_dir.is_dir():
        print(f"Error: Image directory not found: {args.image_dir}", file=sys.stderr)
        sys.exit(1)

    count = export_dataset(args.db, args.image_dir, args.output, args.records)
    if count == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
