#!/usr/bin/env python3
"""Benchmark runner: evaluate multiple Ollama models on the exported dataset.

Sends each image through each specified model using the same extraction prompt
used by the production application, measures extraction time, and saves raw
model outputs for scoring.

Usage:
    python -m benchmark.runner --dataset benchmark/dataset \
        --models qwen3-vl gemma3:27b minicpm-v \
        --endpoint http://localhost:11434 \
        --output benchmark/results

The output directory will contain:
    results/
        run_metadata.json      # Run configuration and timing summary
        raw_outputs/           # Per-model, per-sample raw extraction JSON
            <model>/
                <record_id>.json
        timings/               # Per-model timing data
            <model>.json
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image

# We reuse the exact same prompt and schema simplification from the main app
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from flight_card_scanner.schemas import FlightCardExtraction
from flight_card_scanner.services.extraction_service import (
    EXTRACTION_PROMPT,
    _simplify_schema,
)


DEFAULT_EVENT_START = "April 24, 2026"
DEFAULT_EVENT_END = "April 26, 2026"


def _prepare_image(image_path: Path, target_height: int = 1600) -> str:
    """Read and resize an image, returning base64-encoded JPEG.

    Mirrors the production preprocessing in ExtractionService._call_ollama.
    """
    image_bytes = image_path.read_bytes()
    img = Image.open(BytesIO(image_bytes))

    if img.height > target_height:
        scale = target_height / img.height
        new_width = int(img.width * scale)
        img = img.resize((new_width, target_height), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=90)
        resized_bytes = buf.getvalue()
    else:
        resized_bytes = image_bytes

    return base64.b64encode(resized_bytes).decode("ascii")


def _build_payload(
    model: str,
    b64_image: str,
    event_start: str,
    event_end: str,
) -> dict:
    """Build the Ollama /api/chat payload — identical to production."""
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": EXTRACTION_PROMPT.format(
                    event_start=event_start,
                    event_end=event_end,
                ),
                "images": [b64_image],
            }
        ],
        "format": _simplify_schema(FlightCardExtraction.model_json_schema()),
        "stream": False,
        "options": {"temperature": 0, "num_ctx": 32768, "num_predict": 8192},
        "think": True,
    }


def _extract_content(response_data: dict) -> str | None:
    """Extract the content string from an Ollama response, stripping think blocks."""
    import re

    raw_content = response_data.get("message", {}).get("content", "")
    if not raw_content or not raw_content.strip():
        return None

    cleaned = raw_content.strip()
    if "think>" in cleaned:
        cleaned = re.sub(r".*?</think>", "", cleaned, flags=re.DOTALL).strip()

    return cleaned if cleaned else None


def run_single_extraction(
    client: httpx.Client,
    model: str,
    image_path: Path,
    event_start: str,
    event_end: str,
) -> tuple[dict | None, float, str | None]:
    """Run a single extraction and return (parsed_result, elapsed_seconds, error).

    Returns:
        Tuple of (parsed_dict_or_None, elapsed_seconds, error_message_or_None)
    """
    b64_image = _prepare_image(image_path)
    payload = _build_payload(model, b64_image, event_start, event_end)

    t0 = time.monotonic()
    try:
        response = client.post("/api/chat", json=payload)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        elapsed = time.monotonic() - t0
        return None, elapsed, f"HTTP error: {exc}"

    elapsed = time.monotonic() - t0
    data = response.json()

    content = _extract_content(data)
    if content is None:
        return None, elapsed, "Empty or think-only response"

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        return None, elapsed, f"JSON parse error: {exc}"

    return parsed, elapsed, None


def run_benchmark(
    dataset_dir: Path,
    models: list[str],
    endpoint: str,
    output_dir: Path,
    event_start: str = DEFAULT_EVENT_START,
    event_end: str = DEFAULT_EVENT_END,
    samples: int | None = None,
) -> dict:
    """Run the full benchmark across all models and samples.

    Args:
        dataset_dir: Path to the exported dataset directory.
        models: List of Ollama model names to evaluate.
        endpoint: Ollama endpoint URL.
        output_dir: Directory to write results into.
        event_start: Event start date string for the prompt.
        event_end: Event end date string for the prompt.
        samples: If set, limit to this many samples (for quick testing).

    Returns:
        Run metadata dict with summary statistics.
    """
    # Load manifest
    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest.json not found in {dataset_dir}", file=sys.stderr)
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text())

    if samples is not None:
        manifest = manifest[:samples]

    print(f"Benchmark: {len(manifest)} samples x {len(models)} models = "
          f"{len(manifest) * len(models)} extractions")
    print(f"Endpoint: {endpoint}")
    print()

    # Create output structure
    raw_out = output_dir / "raw_outputs"
    timings_out = output_dir / "timings"
    raw_out.mkdir(parents=True, exist_ok=True)
    timings_out.mkdir(parents=True, exist_ok=True)

    run_metadata = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": endpoint,
        "models": models,
        "num_samples": len(manifest),
        "event_start": event_start,
        "event_end": event_end,
        "model_results": {},
    }

    for model in models:
        print(f"=== Model: {model} ===")

        model_raw_dir = raw_out / model.replace("/", "_").replace(":", "_")
        model_raw_dir.mkdir(parents=True, exist_ok=True)

        model_timings: list[dict] = []
        successes = 0
        failures = 0

        # Use a long timeout since vision models can be slow
        with httpx.Client(
            base_url=endpoint, timeout=httpx.Timeout(600.0)
        ) as client:
            for i, sample in enumerate(manifest, 1):
                record_id = sample["record_id"]
                image_file = dataset_dir / sample["image_file"]

                print(f"  [{i}/{len(manifest)}] Record {record_id}...", end=" ", flush=True)

                result, elapsed, error = run_single_extraction(
                    client, model, image_file, event_start, event_end
                )

                if error:
                    print(f"FAILED ({elapsed:.1f}s): {error}")
                    failures += 1
                    model_timings.append({
                        "record_id": record_id,
                        "elapsed_seconds": elapsed,
                        "success": False,
                        "error": error,
                    })
                    # Save error info
                    out_file = model_raw_dir / f"{record_id}.json"
                    out_file.write_text(json.dumps({
                        "error": error,
                        "elapsed_seconds": elapsed,
                    }, indent=2))
                else:
                    print(f"OK ({elapsed:.1f}s)")
                    successes += 1
                    model_timings.append({
                        "record_id": record_id,
                        "elapsed_seconds": elapsed,
                        "success": True,
                    })
                    # Save raw model output
                    out_file = model_raw_dir / f"{record_id}.json"
                    out_file.write_text(json.dumps(result, indent=2, default=str))

        # Save timings for this model
        timings_file = timings_out / f"{model.replace('/', '_').replace(':', '_')}.json"
        timings_file.write_text(json.dumps(model_timings, indent=2))

        # Compute summary stats
        successful_times = [t["elapsed_seconds"] for t in model_timings if t["success"]]
        summary = {
            "total_samples": len(manifest),
            "successes": successes,
            "failures": failures,
            "total_time_seconds": sum(t["elapsed_seconds"] for t in model_timings),
        }
        if successful_times:
            summary["avg_time_seconds"] = sum(successful_times) / len(successful_times)
            summary["min_time_seconds"] = min(successful_times)
            summary["max_time_seconds"] = max(successful_times)
            summary["median_time_seconds"] = sorted(successful_times)[len(successful_times) // 2]

        run_metadata["model_results"][model] = summary
        print(f"  Summary: {successes}/{len(manifest)} succeeded, "
              f"avg {summary.get('avg_time_seconds', 0):.1f}s per extraction")
        print()

    run_metadata["completed_at"] = datetime.now(timezone.utc).isoformat()

    # Save run metadata
    metadata_file = output_dir / "run_metadata.json"
    metadata_file.write_text(json.dumps(run_metadata, indent=2))

    print(f"Results saved to {output_dir}")
    return run_metadata


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Ollama model extraction benchmark."
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("benchmark/dataset"),
        help="Path to the exported benchmark dataset directory",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        required=True,
        help="Ollama model names to benchmark (e.g., qwen3-vl gemma3:27b)",
    )
    parser.add_argument(
        "--endpoint",
        type=str,
        default="http://localhost:11434",
        help="Ollama endpoint URL (default: http://localhost:11434)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("benchmark/results"),
        help="Output directory for results (default: benchmark/results)",
    )
    parser.add_argument(
        "--event-start",
        type=str,
        default=DEFAULT_EVENT_START,
        help=f"Event start date for prompt (default: {DEFAULT_EVENT_START})",
    )
    parser.add_argument(
        "--event-end",
        type=str,
        default=DEFAULT_EVENT_END,
        help=f"Event end date for prompt (default: {DEFAULT_EVENT_END})",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=None,
        help="Limit to N samples (for quick testing)",
    )

    args = parser.parse_args()

    if not args.dataset.exists():
        print(f"Error: Dataset directory not found: {args.dataset}", file=sys.stderr)
        print("Run 'python -m benchmark.export_dataset' first.", file=sys.stderr)
        sys.exit(1)

    run_benchmark(
        dataset_dir=args.dataset,
        models=args.models,
        endpoint=args.endpoint,
        output_dir=args.output,
        event_start=args.event_start,
        event_end=args.event_end,
        samples=args.samples,
    )


if __name__ == "__main__":
    main()
