#!/usr/bin/env python3
"""Report generator: produce human-readable benchmark comparison reports.

Reads benchmark results and scoring data to produce:
- A summary comparison table across all models
- Per-model detailed breakdowns by field and category
- Timing analysis
- A combined JSON report for programmatic use

Usage:
    python -m benchmark.report --dataset benchmark/dataset \
        --results benchmark/results \
        --output benchmark/report.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from benchmark.scoring import ModelScorecard, score_all_models


def _pct(value: float) -> str:
    """Format a 0-1 float as a percentage string."""
    return f"{value * 100:.1f}%"


def _sec(value: float) -> str:
    """Format seconds with one decimal place."""
    return f"{value:.1f}s"


def generate_markdown_report(
    scorecards: list[ModelScorecard],
    run_metadata: dict,
    output_path: Path | None = None,
) -> str:
    """Generate a Markdown benchmark report.

    Args:
        scorecards: Scored model results, sorted best-to-worst.
        run_metadata: The run_metadata.json contents.
        output_path: If provided, write the report to this file.

    Returns:
        The full Markdown report as a string.
    """
    lines: list[str] = []

    # --- Header ---
    lines.append("# Flight Card Extraction — Model Benchmark Report")
    lines.append("")
    lines.append(f"**Run started:** {run_metadata.get('started_at', 'N/A')}")
    lines.append(f"**Run completed:** {run_metadata.get('completed_at', 'N/A')}")
    lines.append(f"**Endpoint:** {run_metadata.get('endpoint', 'N/A')}")
    lines.append(f"**Samples:** {run_metadata.get('num_samples', 'N/A')}")
    lines.append(f"**Event dates:** {run_metadata.get('event_start', '')} — {run_metadata.get('event_end', '')}")
    lines.append("")

    # --- Overall Comparison Table ---
    lines.append("## Overall Model Comparison")
    lines.append("")
    lines.append("| Rank | Model | Weighted Accuracy | Unweighted Accuracy | Success Rate | Avg Time | Median Time |")
    lines.append("|------|-------|-------------------|---------------------|--------------|----------|-------------|")

    for rank, sc in enumerate(scorecards, 1):
        timing = run_metadata.get("model_results", {}).get(sc.model_name, {})
        avg_time = _sec(timing.get("avg_time_seconds", 0))
        median_time = _sec(timing.get("median_time_seconds", 0))
        lines.append(
            f"| {rank} | {sc.model_name} | {_pct(sc.overall_weighted)} | "
            f"{_pct(sc.overall_unweighted)} | {_pct(sc.extraction_success_rate)} | "
            f"{avg_time} | {median_time} |"
        )

    lines.append("")

    # --- Timing Comparison ---
    lines.append("## Timing Comparison")
    lines.append("")
    lines.append("| Model | Backend | Total Time | Avg Time | Min Time | Max Time | Median Time |")
    lines.append("|-------|---------|------------|----------|----------|----------|-------------|")

    for sc in scorecards:
        timing = run_metadata.get("model_results", {}).get(sc.model_name, {})
        backend = timing.get("backend", "ollama")
        lines.append(
            f"| {sc.model_name} | "
            f"{backend} | "
            f"{_sec(timing.get('total_time_seconds', 0))} | "
            f"{_sec(timing.get('avg_time_seconds', 0))} | "
            f"{_sec(timing.get('min_time_seconds', 0))} | "
            f"{_sec(timing.get('max_time_seconds', 0))} | "
            f"{_sec(timing.get('median_time_seconds', 0))} |"
        )

    lines.append("")

    # --- Token Usage Comparison ---
    # Only show if any model has token data
    has_token_data = any(
        run_metadata.get("model_results", {}).get(sc.model_name, {}).get("total_tokens", 0) > 0
        for sc in scorecards
    )
    if has_token_data:
        lines.append("## Token Usage")
        lines.append("")
        lines.append("| Model | Total Tokens | Avg Input | Avg Output | Avg Total |")
        lines.append("|-------|--------------|-----------|------------|-----------|")

        for sc in scorecards:
            timing = run_metadata.get("model_results", {}).get(sc.model_name, {})
            total_tok = timing.get("total_tokens", 0)
            avg_in = timing.get("avg_input_tokens", 0)
            avg_out = timing.get("avg_output_tokens", 0)
            avg_tot = timing.get("avg_total_tokens", 0)
            lines.append(
                f"| {sc.model_name} | "
                f"{total_tok:,} | "
                f"{avg_in:,.0f} | "
                f"{avg_out:,.0f} | "
                f"{avg_tot:,.0f} |"
            )

        lines.append("")

    # --- Category Breakdown ---
    lines.append("## Accuracy by Category")
    lines.append("")

    # Collect all categories
    all_categories = set()
    for sc in scorecards:
        all_categories.update(sc.category_averages.keys())
    sorted_categories = sorted(all_categories)

    header = "| Model | " + " | ".join(sorted_categories) + " |"
    separator = "|-------| " + " | ".join("---" for _ in sorted_categories) + " |"
    lines.append(header)
    lines.append(separator)

    for sc in scorecards:
        row = f"| {sc.model_name} |"
        for cat in sorted_categories:
            val = sc.category_averages.get(cat, 0.0)
            row += f" {_pct(val)} |"
        lines.append(row)

    lines.append("")

    # --- Per-Field Breakdown ---
    lines.append("## Accuracy by Field")
    lines.append("")

    # Collect all field names
    all_fields = set()
    for sc in scorecards:
        all_fields.update(sc.field_averages.keys())
    sorted_fields = sorted(all_fields)

    header = "| Field | " + " | ".join(sc.model_name for sc in scorecards) + " |"
    separator = "|-------| " + " | ".join("---" for _ in scorecards) + " |"
    lines.append(header)
    lines.append(separator)

    for field_name in sorted_fields:
        row = f"| {field_name} |"
        for sc in scorecards:
            val = sc.field_averages.get(field_name, 0.0)
            row += f" {_pct(val)} |"
        lines.append(row)

    lines.append("")

    # --- Per-Model Detailed Results (worst fields) ---
    lines.append("## Per-Model Weak Points (Fields Below 80%)")
    lines.append("")

    for sc in scorecards:
        weak_fields = [
            (name, score) for name, score in sc.field_averages.items()
            if score < 0.8
        ]
        weak_fields.sort(key=lambda x: x[1])

        if weak_fields:
            lines.append(f"### {sc.model_name}")
            lines.append("")
            lines.append("| Field | Accuracy |")
            lines.append("|-------|----------|")
            for name, score in weak_fields:
                lines.append(f"| {name} | {_pct(score)} |")
            lines.append("")
        else:
            lines.append(f"### {sc.model_name}")
            lines.append("")
            lines.append("All fields at or above 80% accuracy.")
            lines.append("")

    # --- Footer ---
    lines.append("---")
    lines.append("")
    lines.append("*Report generated by `benchmark.report`. "
                 "Weighted accuracy prioritizes critical fields (flier_name, motors, "
                 "evaluation_outcome) over supplementary fields (notes, rocket_colors).*")

    report = "\n".join(lines)

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report)
        print(f"Report written to {output_path}")

    return report


def generate_json_report(
    scorecards: list[ModelScorecard],
    run_metadata: dict,
    output_path: Path | None = None,
) -> dict:
    """Generate a structured JSON report for programmatic use.

    Args:
        scorecards: Scored model results.
        run_metadata: The run_metadata.json contents.
        output_path: If provided, write the JSON report to this file.

    Returns:
        The report as a dict.
    """
    report = {
        "run_metadata": run_metadata,
        "rankings": [],
    }

    for rank, sc in enumerate(scorecards, 1):
        timing = run_metadata.get("model_results", {}).get(sc.model_name, {})
        entry = {
            "rank": rank,
            "model": sc.model_name,
            "overall_weighted_accuracy": round(sc.overall_weighted, 4),
            "overall_unweighted_accuracy": round(sc.overall_unweighted, 4),
            "extraction_success_rate": round(sc.extraction_success_rate, 4),
            "timing": {
                "avg_seconds": timing.get("avg_time_seconds"),
                "median_seconds": timing.get("median_time_seconds"),
                "min_seconds": timing.get("min_time_seconds"),
                "max_seconds": timing.get("max_time_seconds"),
                "total_seconds": timing.get("total_time_seconds"),
            },
            "tokens": {
                "total_input": timing.get("total_input_tokens"),
                "total_output": timing.get("total_output_tokens"),
                "total": timing.get("total_tokens"),
                "avg_input": timing.get("avg_input_tokens"),
                "avg_output": timing.get("avg_output_tokens"),
                "avg_total": timing.get("avg_total_tokens"),
            },
            "field_accuracy": {
                name: round(score, 4)
                for name, score in sorted(sc.field_averages.items())
            },
            "category_accuracy": {
                name: round(score, 4)
                for name, score in sorted(sc.category_averages.items())
            },
            "per_sample": [
                {
                    "record_id": sr.record_id,
                    "weighted_score": round(sr.weighted_score, 4),
                    "extraction_success": sr.extraction_success,
                }
                for sr in sc.sample_results
            ],
        }
        report["rankings"].append(entry)

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2))
        print(f"JSON report written to {output_path}")

    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate benchmark comparison report."
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        required=True,
        help="Path to the benchmark dataset directory (output of export step)",
    )
    parser.add_argument(
        "--results",
        type=Path,
        required=True,
        help="Path to the benchmark results directory (output of run step)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output path for the Markdown report",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=None,
        help="Optional: also output a JSON report to this path",
    )

    args = parser.parse_args()

    if not args.dataset.exists():
        print(f"Error: Dataset directory not found: {args.dataset}", file=sys.stderr)
        sys.exit(1)

    if not args.results.exists():
        print(f"Error: Results directory not found: {args.results}", file=sys.stderr)
        print("Run 'python -m benchmark.runner' first.", file=sys.stderr)
        sys.exit(1)

    # Load run metadata
    metadata_path = args.results / "run_metadata.json"
    if not metadata_path.exists():
        print(f"Error: run_metadata.json not found in {args.results}", file=sys.stderr)
        sys.exit(1)
    run_metadata = json.loads(metadata_path.read_text())

    # Score all models
    print("Scoring models...")
    scorecards = score_all_models(args.dataset, args.results)

    # Generate reports
    report_md = generate_markdown_report(scorecards, run_metadata, args.output)

    if args.json_output:
        generate_json_report(scorecards, run_metadata, args.json_output)

    # Print summary to console
    print()
    print("=" * 72)
    print("BENCHMARK SUMMARY")
    print("=" * 72)
    print(f"{'Model':<25} {'Weighted':<12} {'Avg Time':<10} {'Avg Tokens':<12} {'Success':<10}")
    print("-" * 72)
    for sc in scorecards:
        timing = run_metadata.get("model_results", {}).get(sc.model_name, {})
        avg_time = timing.get("avg_time_seconds", 0)
        avg_tokens = timing.get("avg_total_tokens", 0)
        tokens_str = f"{avg_tokens:,.0f}" if avg_tokens else "N/A"
        print(
            f"{sc.model_name:<25} {_pct(sc.overall_weighted):<12} "
            f"{_sec(avg_time):<10} {tokens_str:<12} {_pct(sc.extraction_success_rate):<10}"
        )
    print("=" * 72)


if __name__ == "__main__":
    main()
