#!/usr/bin/env python3
"""Benchmark runner: evaluate multiple Ollama and Bedrock models on the exported dataset.

Sends each image through each specified model using the same extraction prompt
used by the production application, measures extraction time, captures thinking
traces, tracks token usage, and saves raw model outputs for scoring.

Model specification:
  - Ollama models: plain name like "qwen3-vl", "gemma3:27b"
  - Bedrock models: prefix with "bedrock:" like "bedrock:us-east-1:us.amazon.nova-pro-v1:0"
    Format: bedrock:<region>:<model_id>

Usage:
    # Ollama models
    python -m benchmark.runner --dataset /path/to/dataset \
        --models qwen3-vl gemma3:27b \
        --endpoint http://localhost:11434 \
        --output /path/to/results

    # Bedrock models (no --endpoint needed)
    python -m benchmark.runner --dataset /path/to/dataset \
        --models "bedrock:us-east-1:us.amazon.nova-pro-v1:0" \
        --output /path/to/results

    # Mix of both
    python -m benchmark.runner --dataset /path/to/dataset \
        --models qwen3-vl "bedrock:us-east-1:us.amazon.nova-pro-v1:0" \
        --endpoint http://localhost:11434 \
        --output /path/to/results

    # Save thinking traces for analysis
    python -m benchmark.runner --dataset /path/to/dataset \
        --models qwen3-vl --output /path/to/results --save-thinking

The output directory will contain:
    results/
        run_metadata.json          # Run configuration and timing summary
        <model>/                   # One directory per model
            timings.json           # Per-sample timing + token data
            raw_outputs/           # Per-sample raw extraction JSON
                <record_id>.json
            thinking/              # Per-sample thinking traces (if --save-thinking)
                <record_id>.txt
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from dataclasses import dataclass, field

import httpx
from PIL import Image

# We reuse the exact same prompt and schema simplification from the main app
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from flight_card_scanner.schemas import FlightCardExtraction
from flight_card_scanner.services.extraction_service import (
    EXTRACTION_PROMPT,
    _simplify_schema,
)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ExtractionResult:
    """Result of a single extraction attempt."""
    parsed: dict | None = None
    elapsed_seconds: float = 0.0
    error: str | None = None
    thinking: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _warn_if_inside_repo(output_dir: Path) -> None:
    """Warn the user if the output path appears to be inside a git repository tree."""
    check = output_dir.resolve()
    while check != check.parent:
        if (check / ".git").exists():
            print(
                f"  WARNING: Output path is inside a git repository ({check}).\n"
                f"  Consider writing benchmark results outside the source tree to avoid "
                f"accidentally committing large files.",
                file=sys.stderr,
            )
            return
        check = check.parent


def _prepare_image_b64(image_path: Path, target_height: int = 1600) -> str:
    """Read and resize an image, returning base64-encoded JPEG string."""
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


def _prepare_image_bytes(image_path: Path, target_height: int = 1600) -> bytes:
    """Read and resize an image, returning raw JPEG bytes (for Bedrock)."""
    image_bytes = image_path.read_bytes()
    img = Image.open(BytesIO(image_bytes))

    if img.height > target_height:
        scale = target_height / img.height
        new_width = int(img.width * scale)
        img = img.resize((new_width, target_height), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()
    else:
        return image_bytes


def _is_bedrock_model(model: str) -> bool:
    """Check if a model string specifies a Bedrock model (bedrock:<region>:<model_id>)."""
    return model.startswith("bedrock:")


def _parse_bedrock_model(model: str) -> tuple[str, str]:
    """Parse a bedrock model string into (region, model_id).

    Format: bedrock:<region>:<model_id>
    Example: bedrock:us-east-1:us.amazon.nova-pro-v1:0
    """
    parts = model.split(":", 2)
    if len(parts) < 3:
        raise ValueError(
            f"Invalid Bedrock model format: {model!r}. "
            f"Expected: bedrock:<region>:<model_id>"
        )
    region = parts[1]
    model_id = parts[2]
    return region, model_id


def _model_dir_name(model: str) -> str:
    """Convert a model name to a safe directory name."""
    return model.replace("/", "_").replace(":", "_")


# ---------------------------------------------------------------------------
# Ollama backend
# ---------------------------------------------------------------------------

def _probe_thinking_support(client: httpx.Client, model: str) -> bool:
    """Probe whether an Ollama model supports the think parameter."""
    THINKING_PREFIXES = (
        "qwen3",
        "deepseek-r1",
        "deepseek-v3",
        "gpt-oss",
    )

    try:
        resp = client.post("/api/show", json={"model": model})
        if resp.status_code == 200:
            data = resp.json()
            capabilities = data.get("capabilities", [])
            if capabilities:
                return "thinking" in capabilities
            model_info = data.get("model_info", {})
            if model_info:
                caps = model_info.get("capabilities", [])
                if caps:
                    return "thinking" in caps
    except Exception:
        pass

    model_lower = model.lower()
    return any(model_lower.startswith(prefix) for prefix in THINKING_PREFIXES)


def run_ollama_extraction(
    client: httpx.Client,
    model: str,
    image_path: Path,
    event_start: str,
    event_end: str,
    think: bool | None = None,
) -> ExtractionResult:
    """Run a single extraction via Ollama and return structured result."""
    b64_image = _prepare_image_b64(image_path)

    payload = {
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
    }

    if think is not None:
        payload["think"] = think

    t0 = time.monotonic()
    try:
        response = client.post("/api/chat", json=payload)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        elapsed = time.monotonic() - t0
        try:
            err_body = exc.response.json()
            err_msg = err_body.get("error", str(exc))
        except Exception:
            err_msg = str(exc)
        return ExtractionResult(elapsed_seconds=elapsed, error=f"HTTP {exc.response.status_code}: {err_msg}")
    except httpx.HTTPError as exc:
        elapsed = time.monotonic() - t0
        return ExtractionResult(elapsed_seconds=elapsed, error=f"HTTP error: {exc}")

    elapsed = time.monotonic() - t0
    data = response.json()

    # Extract token usage from Ollama response
    input_tokens = data.get("prompt_eval_count")
    output_tokens = data.get("eval_count")
    total_tokens = None
    if input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens

    # Extract thinking trace (from the dedicated field or embedded in content)
    thinking = data.get("message", {}).get("thinking")

    # Extract content, stripping think blocks
    raw_content = data.get("message", {}).get("content", "")
    if not raw_content or not raw_content.strip():
        return ExtractionResult(
            elapsed_seconds=elapsed, error="Empty or think-only response",
            thinking=thinking, input_tokens=input_tokens,
            output_tokens=output_tokens, total_tokens=total_tokens,
        )

    cleaned = raw_content.strip()
    # If thinking wasn't in the dedicated field, try to extract from content
    if thinking is None and "think>" in cleaned:
        think_match = re.search(r"<think>(.*?)</think>", cleaned, re.DOTALL)
        if think_match:
            thinking = think_match.group(1).strip()

    if "think>" in cleaned:
        cleaned = re.sub(r".*?</think>", "", cleaned, flags=re.DOTALL).strip()

    if not cleaned:
        return ExtractionResult(
            elapsed_seconds=elapsed, error="Content was only a think block with no JSON",
            thinking=thinking, input_tokens=input_tokens,
            output_tokens=output_tokens, total_tokens=total_tokens,
        )

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return ExtractionResult(
            elapsed_seconds=elapsed, error=f"JSON parse error: {exc}",
            thinking=thinking, input_tokens=input_tokens,
            output_tokens=output_tokens, total_tokens=total_tokens,
        )

    return ExtractionResult(
        parsed=parsed, elapsed_seconds=elapsed, thinking=thinking,
        input_tokens=input_tokens, output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


# ---------------------------------------------------------------------------
# Bedrock backend
# ---------------------------------------------------------------------------

def run_bedrock_extraction(
    bedrock_client,
    model_id: str,
    image_path: Path,
    event_start: str,
    event_end: str,
) -> ExtractionResult:
    """Run a single extraction via Amazon Bedrock Converse API.

    Uses the same approach as the production code: embeds the JSON schema
    in the prompt text since Bedrock doesn't support Ollama's native
    structured output format parameter.
    """
    image_bytes = _prepare_image_bytes(image_path)

    prompt_text = EXTRACTION_PROMPT.format(
        event_start=event_start,
        event_end=event_end,
    )

    # Append JSON schema to prompt (Bedrock doesn't have structured output support)
    schema = _simplify_schema(FlightCardExtraction.model_json_schema())
    prompt_text += (
        "\n\nYou MUST respond with a single JSON object conforming to this schema:\n"
        + json.dumps(schema)
        + "\n\nRespond ONLY with valid JSON. No markdown fences, no explanation."
    )

    t0 = time.monotonic()
    try:
        response = bedrock_client.converse(
            modelId=model_id,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "image": {
                                "format": "jpeg",
                                "source": {"bytes": image_bytes},
                            }
                        },
                        {"text": prompt_text},
                    ],
                }
            ],
            inferenceConfig={
                "temperature": 0.0,
                "maxTokens": 8192,
            },
        )
    except Exception as exc:
        elapsed = time.monotonic() - t0
        return ExtractionResult(elapsed_seconds=elapsed, error=f"Bedrock API error: {exc}")

    elapsed = time.monotonic() - t0

    # Extract token usage from Bedrock response
    usage = response.get("usage", {})
    input_tokens = usage.get("inputTokens")
    output_tokens = usage.get("outputTokens")
    total_tokens = usage.get("totalTokens")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens

    # Extract content and thinking from Bedrock Converse response
    # Content blocks may include "thinking" type blocks and "text" type blocks
    thinking = None
    text_parts = []

    try:
        content_blocks = response["output"]["message"]["content"]
        for block in content_blocks:
            if "thinking" in block:
                # Bedrock thinking block
                thinking = block["thinking"].get("text", "")
            elif "text" in block:
                text_parts.append(block["text"])
    except (KeyError, IndexError, TypeError) as exc:
        return ExtractionResult(
            elapsed_seconds=elapsed,
            error=f"Unexpected Bedrock response structure: {exc}",
            input_tokens=input_tokens, output_tokens=output_tokens,
            total_tokens=total_tokens,
        )

    raw_content = "\n".join(text_parts) if text_parts else ""

    if not raw_content or not raw_content.strip():
        return ExtractionResult(
            elapsed_seconds=elapsed, error="Bedrock returned empty content",
            thinking=thinking, input_tokens=input_tokens,
            output_tokens=output_tokens, total_tokens=total_tokens,
        )

    # Strip think blocks that may be embedded in text content
    cleaned = raw_content.strip()
    if thinking is None and "think>" in cleaned:
        think_match = re.search(r"<think>(.*?)</think>", cleaned, re.DOTALL)
        if think_match:
            thinking = think_match.group(1).strip()

    if "think>" in cleaned:
        cleaned = re.sub(r".*?</think>", "", cleaned, flags=re.DOTALL).strip()

    if not cleaned:
        return ExtractionResult(
            elapsed_seconds=elapsed, error="Content was only a think block with no JSON",
            thinking=thinking, input_tokens=input_tokens,
            output_tokens=output_tokens, total_tokens=total_tokens,
        )

    # Strip markdown code fences if present
    json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", cleaned, re.DOTALL)
    if json_match:
        cleaned = json_match.group(1).strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return ExtractionResult(
            elapsed_seconds=elapsed, error=f"JSON parse error: {exc}",
            thinking=thinking, input_tokens=input_tokens,
            output_tokens=output_tokens, total_tokens=total_tokens,
        )

    return ExtractionResult(
        parsed=parsed, elapsed_seconds=elapsed, thinking=thinking,
        input_tokens=input_tokens, output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


# ---------------------------------------------------------------------------
# Main benchmark runner
# ---------------------------------------------------------------------------

def run_benchmark(
    dataset_dir: Path,
    models: list[str],
    endpoint: str,
    output_dir: Path,
    event_start: str | None = None,
    event_end: str | None = None,
    samples: int | None = None,
    save_thinking: bool = False,
) -> dict:
    """Run the full benchmark across all models and samples.

    Args:
        dataset_dir: Path to the exported dataset directory.
        models: List of model identifiers (Ollama names or bedrock:<region>:<model_id>).
        endpoint: Ollama endpoint URL (used for Ollama models only).
        output_dir: Directory to write results into.
        event_start: Event start date string for the prompt. If None, read from manifest.
        event_end: Event end date string for the prompt. If None, read from manifest.
        samples: If set, limit to this many samples (for quick testing).
        save_thinking: If True, save thinking traces to separate files.

    Returns:
        Run metadata dict with summary statistics.
    """
    _warn_if_inside_repo(output_dir)

    # Load manifest
    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest.json not found in {dataset_dir}", file=sys.stderr)
        sys.exit(1)

    manifest_data = json.loads(manifest_path.read_text())

    # Read event date range from manifest (written by export step)
    date_range = manifest_data.get("event_date_range", {})
    if event_start is None:
        event_start = date_range.get("start")
    if event_end is None:
        event_end = date_range.get("end")

    if not event_start or not event_end:
        print(
            "Error: Event date range not found in manifest and not provided via CLI.\n"
            "Re-export the dataset or pass --event-start and --event-end.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Convert ISO dates to human-readable format for the prompt
    from datetime import date as _date
    try:
        start_dt = _date.fromisoformat(event_start)
        event_start_fmt = start_dt.strftime("%B %-d, %Y")
    except ValueError:
        event_start_fmt = event_start
    try:
        end_dt = _date.fromisoformat(event_end)
        event_end_fmt = end_dt.strftime("%B %-d, %Y")
    except ValueError:
        event_end_fmt = event_end

    sample_list = manifest_data.get("samples", manifest_data)
    if isinstance(sample_list, dict):
        sample_list = sample_list.get("samples", [])

    if samples is not None:
        sample_list = sample_list[:samples]

    print(f"Benchmark: {len(sample_list)} samples x {len(models)} models = "
          f"{len(sample_list) * len(models)} extractions")
    print(f"Ollama endpoint: {endpoint}")
    print(f"Event dates: {event_start} to {event_end}")
    print(f"Save thinking: {save_thinking}")
    print()

    # Create output structure
    output_dir.mkdir(parents=True, exist_ok=True)

    run_metadata = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": endpoint,
        "models": models,
        "num_samples": len(sample_list),
        "event_start": event_start,
        "event_end": event_end,
        "save_thinking": save_thinking,
        "model_results": {},
    }

    for model in models:
        print(f"=== Model: {model} ===")

        dir_name = _model_dir_name(model)
        model_dir = output_dir / dir_name
        model_raw_dir = model_dir / "raw_outputs"
        model_raw_dir.mkdir(parents=True, exist_ok=True)

        if save_thinking:
            model_thinking_dir = model_dir / "thinking"
            model_thinking_dir.mkdir(parents=True, exist_ok=True)

        model_timings: list[dict] = []
        successes = 0
        failures = 0
        total_input_tokens = 0
        total_output_tokens = 0
        total_all_tokens = 0

        is_bedrock = _is_bedrock_model(model)

        if is_bedrock:
            region, model_id = _parse_bedrock_model(model)
            print(f"  Backend: Bedrock ({region}, {model_id})")

            import boto3
            bedrock_client = boto3.client("bedrock-runtime", region_name=region)

            for i, sample in enumerate(sample_list, 1):
                record_id = sample["record_id"]
                image_file = dataset_dir / sample["image_file"]
                print(f"  [{i}/{len(sample_list)}] Record {record_id}...", end=" ", flush=True)

                result = run_bedrock_extraction(
                    bedrock_client, model_id, image_file,
                    event_start_fmt, event_end_fmt,
                )

                _process_result(
                    result, record_id, model_raw_dir,
                    model_dir / "thinking" if save_thinking else None,
                    model_timings, save_thinking,
                )

                if result.error:
                    failures += 1
                else:
                    successes += 1

                if result.input_tokens:
                    total_input_tokens += result.input_tokens
                if result.output_tokens:
                    total_output_tokens += result.output_tokens
                if result.total_tokens:
                    total_all_tokens += result.total_tokens

        else:
            # Ollama backend
            print(f"  Backend: Ollama ({endpoint})")

            with httpx.Client(
                base_url=endpoint, timeout=httpx.Timeout(600.0)
            ) as client:
                supports_think = _probe_thinking_support(client, model)
                think_param = True if supports_think else None
                if supports_think:
                    print(f"  Thinking: enabled (model supports it)")
                else:
                    print(f"  Thinking: omitted (model does not support it)")

                for i, sample in enumerate(sample_list, 1):
                    record_id = sample["record_id"]
                    image_file = dataset_dir / sample["image_file"]
                    print(f"  [{i}/{len(sample_list)}] Record {record_id}...", end=" ", flush=True)

                    result = run_ollama_extraction(
                        client, model, image_file,
                        event_start_fmt, event_end_fmt,
                        think=think_param,
                    )

                    _process_result(
                        result, record_id, model_raw_dir,
                        model_dir / "thinking" if save_thinking else None,
                        model_timings, save_thinking,
                    )

                    if result.error:
                        failures += 1
                    else:
                        successes += 1

                    if result.input_tokens:
                        total_input_tokens += result.input_tokens
                    if result.output_tokens:
                        total_output_tokens += result.output_tokens
                    if result.total_tokens:
                        total_all_tokens += result.total_tokens

        # Save timings for this model
        timings_file = model_dir / "timings.json"
        timings_file.write_text(json.dumps(model_timings, indent=2))

        # Compute summary stats
        successful_times = [t["elapsed_seconds"] for t in model_timings if t["success"]]
        summary: dict = {
            "total_samples": len(sample_list),
            "successes": successes,
            "failures": failures,
            "backend": "bedrock" if is_bedrock else "ollama",
            "total_time_seconds": sum(t["elapsed_seconds"] for t in model_timings),
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tokens": total_all_tokens,
        }
        if successful_times:
            summary["avg_time_seconds"] = sum(successful_times) / len(successful_times)
            summary["min_time_seconds"] = min(successful_times)
            summary["max_time_seconds"] = max(successful_times)
            summary["median_time_seconds"] = sorted(successful_times)[len(successful_times) // 2]
        if successes > 0:
            summary["avg_input_tokens"] = total_input_tokens / successes
            summary["avg_output_tokens"] = total_output_tokens / successes
            summary["avg_total_tokens"] = total_all_tokens / successes

        if not is_bedrock:
            summary["thinking_enabled"] = supports_think

        run_metadata["model_results"][model] = summary

        tokens_str = f", {total_all_tokens} total tokens" if total_all_tokens else ""
        print(f"  Summary: {successes}/{len(sample_list)} succeeded, "
              f"avg {summary.get('avg_time_seconds', 0):.1f}s per extraction{tokens_str}")
        print()

    run_metadata["completed_at"] = datetime.now(timezone.utc).isoformat()

    # Save run metadata
    metadata_file = output_dir / "run_metadata.json"
    metadata_file.write_text(json.dumps(run_metadata, indent=2))

    print(f"Results saved to {output_dir}")
    return run_metadata


def _process_result(
    result: ExtractionResult,
    record_id: int,
    raw_dir: Path,
    thinking_dir: Path | None,
    timings: list[dict],
    save_thinking: bool,
) -> None:
    """Process an extraction result: print status, save files, append timing."""
    elapsed = result.elapsed_seconds

    timing_entry: dict = {
        "record_id": record_id,
        "elapsed_seconds": elapsed,
        "success": result.error is None,
    }
    if result.input_tokens is not None:
        timing_entry["input_tokens"] = result.input_tokens
    if result.output_tokens is not None:
        timing_entry["output_tokens"] = result.output_tokens
    if result.total_tokens is not None:
        timing_entry["total_tokens"] = result.total_tokens

    if result.error:
        print(f"FAILED ({elapsed:.1f}s): {result.error}")
        timing_entry["error"] = result.error
        out_file = raw_dir / f"{record_id}.json"
        out_file.write_text(json.dumps({
            "error": result.error,
            "elapsed_seconds": elapsed,
        }, indent=2))
    else:
        tokens_info = ""
        if result.total_tokens:
            tokens_info = f", {result.total_tokens} tok"
        print(f"OK ({elapsed:.1f}s{tokens_info})")
        out_file = raw_dir / f"{record_id}.json"
        out_file.write_text(json.dumps(result.parsed, indent=2, default=str))

    # Save thinking trace if requested
    if save_thinking and thinking_dir and result.thinking:
        think_file = thinking_dir / f"{record_id}.txt"
        think_file.write_text(result.thinking)

    timings.append(timing_entry)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run model extraction benchmark (supports Ollama and Bedrock)."
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        required=True,
        help="Path to the exported benchmark dataset directory",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        required=True,
        help=(
            "Model identifiers to benchmark. Ollama: plain names (e.g., qwen3-vl). "
            "Bedrock: bedrock:<region>:<model_id> (e.g., bedrock:us-east-1:us.amazon.nova-pro-v1:0)"
        ),
    )
    parser.add_argument(
        "--endpoint",
        type=str,
        default="http://localhost:11434",
        help="Ollama endpoint URL (default: http://localhost:11434). Ignored for Bedrock models.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output directory where benchmark results will be written",
    )
    parser.add_argument(
        "--event-start",
        type=str,
        default=None,
        help="Override event start date for prompt (default: read from dataset manifest)",
    )
    parser.add_argument(
        "--event-end",
        type=str,
        default=None,
        help="Override event end date for prompt (default: read from dataset manifest)",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=None,
        help="Limit to N samples (for quick testing)",
    )
    parser.add_argument(
        "--save-thinking",
        action="store_true",
        default=False,
        help=(
            "Save model thinking/reasoning traces to separate files for analysis. "
            "Omit for pure performance assessment (traces are discarded)."
        ),
    )

    args = parser.parse_args()

    if not args.dataset.exists():
        print(f"Error: Dataset directory not found: {args.dataset}", file=sys.stderr)
        print("Run 'python -m benchmark export' first.", file=sys.stderr)
        sys.exit(1)

    run_benchmark(
        dataset_dir=args.dataset,
        models=args.models,
        endpoint=args.endpoint,
        output_dir=args.output,
        event_start=args.event_start,
        event_end=args.event_end,
        samples=args.samples,
        save_thinking=args.save_thinking,
    )


if __name__ == "__main__":
    main()
