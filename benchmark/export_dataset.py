#!/usr/bin/env python3
"""Export human-verified flight card records into a benchmark dataset.

Reads the project's SQLite database, finds all records marked as human_verified,
and exports:
  - The original card image (copied to the dataset directory)
  - A JSON ground truth file containing the verified field values

Usage:
    python -m benchmark.export_dataset --db path/to/flight_cards.db \
        --image-dir path/to/images --output benchmark/dataset

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


def _load_verified_records(db_path: Path) -> list[dict]:
    """Query the database for all human-verified records with extracted status."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            id, image_path, flight_date, flier_name,
            total_impulse_value, total_impulse_unit,
            flag_heads_up, flag_first_flight, flag_complex,
            rack, pad, fso_rso_initials,
            evaluation_outcome, evaluation_comments,
            recovery_plan, overflow
        FROM flight_records
        WHERE human_verified = 1 AND extraction_status = 'extracted'
    """)

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


def export_dataset(
    db_path: Path,
    image_dir: Path,
    output_dir: Path,
) -> int:
    """Export verified records to a benchmark dataset.

    Args:
        db_path: Path to the SQLite database file.
        image_dir: Path to the image store directory (where image_path is relative to).
        output_dir: Directory to write the dataset into.

    Returns:
        Number of records exported.
    """
    records = _load_verified_records(db_path)

    if not records:
        print("No human-verified records found in the database.", file=sys.stderr)
        return 0

    # Create output structure
    images_out = output_dir / "images"
    gt_out = output_dir / "ground_truth"
    images_out.mkdir(parents=True, exist_ok=True)
    gt_out.mkdir(parents=True, exist_ok=True)

    manifest = []
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

        manifest.append({
            "record_id": record_id,
            "image_file": f"images/{record_id}{ext}",
            "ground_truth_file": f"ground_truth/{record_id}.json",
            "flier_name": record["flier_name"],
            "flight_date": record["flight_date"],
        })
        exported += 1

    # Write manifest
    manifest_file = output_dir / "manifest.json"
    manifest_file.write_text(json.dumps(manifest, indent=2, default=str))

    print(f"Exported {exported} verified records to {output_dir}")
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
        default=Path("benchmark/dataset"),
        help="Output directory for the benchmark dataset (default: benchmark/dataset)",
    )

    args = parser.parse_args()

    if not args.db.exists():
        print(f"Error: Database not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    if not args.image_dir.is_dir():
        print(f"Error: Image directory not found: {args.image_dir}", file=sys.stderr)
        sys.exit(1)

    count = export_dataset(args.db, args.image_dir, args.output)
    if count == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
