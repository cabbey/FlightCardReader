# Flight Card Extraction — Model Benchmark Suite

Evaluate and compare Ollama vision models on flight card data extraction accuracy and speed.

This benchmark uses the **exact same prompt, schema, and image preprocessing** as the production
application, ensuring results directly reflect real-world performance.

## Overview

The benchmark has three stages:

1. **Export** — Extract human-verified records from the project database into a portable dataset
2. **Run** — Send each image to multiple Ollama models and collect raw outputs + timing
3. **Score** — Compare model outputs against ground truth and generate a report

## Prerequisites

- Python 3.11+
- The project's dependencies installed (`httpx`, `Pillow`, `pydantic`)
- An Ollama instance running with the models you want to benchmark already pulled
- A database with human-verified records (records where `human_verified = 1`)

## Quick Start

```bash
# From the project root directory:

# 1. Export verified records from your database
python -m benchmark export \
    --db /path/to/event/data/flight_cards.db \
    --image-dir /path/to/event/data/images \
    --output /path/to/benchmark/dataset

# 1b. Or export only specific record IDs
python -m benchmark export \
    --db /path/to/event/data/flight_cards.db \
    --image-dir /path/to/event/data/images \
    --output /path/to/benchmark/dataset \
    --records 1 5 12 47 63

# 2. Run the benchmark against multiple models
python -m benchmark run \
    --dataset /path/to/benchmark/dataset \
    --models qwen3-vl gemma3:27b minicpm-v \
    --endpoint http://localhost:11434 \
    --output /path/to/benchmark/results

# 3. Score results and generate the comparison report
python -m benchmark score \
    --dataset /path/to/benchmark/dataset \
    --results /path/to/benchmark/results \
    --output /path/to/benchmark/report.md
```

## Detailed Usage

### Step 1: Export Dataset

```bash
python -m benchmark export \
    --db <path-to-flight_cards.db> \
    --image-dir <path-to-images-directory> \
    --output <output-directory> \
    [--records <id1> <id2> ...]
```

**Arguments:**
| Argument | Required | Description |
|----------|----------|-------------|
| `--db` | Yes | Path to the SQLite database file |
| `--image-dir` | Yes | Path to the image store directory (parent of `image_path` values in DB) |
| `--output` | Yes | Output directory where the dataset will be written |
| `--records` | No | Specific record IDs to export (default: all human-verified records) |

**Output structure:**
```
dataset/
├── manifest.json          # List of all samples with metadata
├── images/                # Copies of the original card images
│   ├── 1.jpg
│   ├── 2.jpg
│   └── ...
└── ground_truth/          # Human-verified extraction data
    ├── 1.json
    ├── 2.json
    └── ...
```

The dataset is self-contained and portable — you can copy it to another machine for benchmarking.

### Step 2: Run Benchmark

```bash
python -m benchmark run \
    --dataset <dataset-directory> \
    --models <model1> [model2] ... \
    --endpoint <ollama-url> \
    --output <results-directory> \
    [--event-start "2026-04-24"] \
    [--event-end "2026-04-26"] \
    [--samples N]
```

**Arguments:**
| Argument | Required | Description |
|----------|----------|-------------|
| `--dataset` | Yes | Path to exported dataset (from step 1) |
| `--models` | Yes | Space-separated list of Ollama model names |
| `--endpoint` | No | Ollama API URL (default: `http://localhost:11434`) |
| `--output` | Yes | Results output directory |
| `--event-start` | No | Override event start date (default: read from dataset manifest) |
| `--event-end` | No | Override event end date (default: read from dataset manifest) |
| `--samples` | No | Limit to N samples (useful for quick testing) |

**Output structure:**
```
results/
├── run_metadata.json          # Run configuration, timing summaries
├── qwen3-vl/                  # One directory per model
│   ├── timings.json           # Per-sample timing data
│   └── raw_outputs/           # Raw extraction JSON per sample
│       ├── 1.json
│       └── ...
├── gemma3_27b/
│   ├── timings.json
│   └── raw_outputs/
│       └── ...
└── ...
```

**Tips:**
- Use `--samples 5` for a quick sanity check before a full run
- Models must already be pulled in Ollama (`ollama pull <model>`)
- The benchmark uses a 10-minute timeout per extraction (vision models can be slow)
- Run on the same hardware for fair timing comparisons

### Step 3: Score & Report

```bash
python -m benchmark score \
    --dataset <dataset-directory> \
    --results <results-directory> \
    --output <report-path.md> \
    [--json-output <report-path.json>]
```

**Arguments:**
| Argument | Required | Description |
|----------|----------|-------------|
| `--dataset` | Yes | Path to benchmark dataset (from step 1) |
| `--results` | Yes | Path to benchmark results (from step 2) |
| `--output` | Yes | Markdown report output path |
| `--json-output` | No | Optional JSON report for programmatic use |

## Understanding the Report

### Scoring Methodology

Each field extracted by the model is compared against the human-verified ground truth using
type-appropriate comparison strategies:

| Field Type | Comparison Method |
|------------|-------------------|
| Strings | Case-insensitive exact match (with fuzzy partial credit for substrings) |
| Numbers | Exact for integers; 1% relative tolerance for floats |
| Booleans | Exact match |
| Motors | Weighted component comparison (letter > number > manufacturer > suffix > quantity); bonus for proper cluster collapsing |
| Measurements | Per-dimension value + unit comparison |
| Membership | Club + member number + cert level comparison |
| String lists | Jaccard similarity (order-insensitive) |

### Weighted vs Unweighted Accuracy

- **Weighted accuracy** prioritizes critical fields that matter most for record keeping:
  - High weight (3x): `flier_name`, `motors`
  - Medium weight (2x): `flight_date`, `evaluation_outcome`, `membership`
  - Standard weight (1.5x): `rack`, `pad`, `fso_rso_initials`, `recovery_plan`
  - Low weight (0.5-1x): `notes`, `rocket_colors`, `rocket_manufacturer`

- **Unweighted accuracy** treats all fields equally (simple average)

### Report Sections

1. **Overall Model Comparison** — Ranked table with accuracy and timing
2. **Timing Comparison** — Detailed timing statistics per model
3. **Accuracy by Category** — Scores grouped by field category (identity, technical, operational, etc.)
4. **Accuracy by Field** — Every field compared across all models
5. **Per-Model Weak Points** — Fields where each model scores below 80%

## Example Workflow

```bash
# Pull models you want to test
ollama pull qwen3-vl
ollama pull gemma3:27b
ollama pull minicpm-v

# Export your verified data
python -m benchmark export \
    --db /home/user/events/2026/march/flight_cards.db \
    --image-dir /home/user/events/2026/march/images \
    --output /home/user/benchmarks/dataset

# Quick test with 3 samples
python -m benchmark run \
    --dataset /home/user/benchmarks/dataset \
    --models qwen3-vl gemma3:27b minicpm-v \
    --output /home/user/benchmarks/results \
    --samples 3

# Full benchmark (may take a while depending on hardware and sample count)
python -m benchmark run \
    --dataset /home/user/benchmarks/dataset \
    --models qwen3-vl gemma3:27b minicpm-v \
    --output /home/user/benchmarks/results

# Generate comparison report
python -m benchmark score \
    --dataset /home/user/benchmarks/dataset \
    --results /home/user/benchmarks/results \
    --output /home/user/benchmarks/report.md \
    --json-output /home/user/benchmarks/report.json
```

## Output Path Warnings

Both `export` and `run` will emit a warning if the output path is inside a git repository.
Benchmark data (images, raw model outputs) can be large and should generally live **outside**
the source tree to avoid accidentally committing them. Choose a path like
`/home/user/benchmarks/` or another data directory.

## Adding the Dataset to Version Control

If you do choose to store the dataset inside the repo tree (despite the warning above),
consider:
- Adding the output directory to `.gitignore`
- Using Git LFS for the images directory
- Keeping just the `manifest.json` and `ground_truth/` in version control

## Re-running After Prompt Changes

If you modify the extraction prompt in the main application, you can re-run the benchmark
to see how the change affects each model:

```bash
# Results are saved per-run, so use a different output directory to compare
python -m benchmark run \
    --dataset /path/to/benchmark/dataset \
    --models qwen3-vl \
    --output /path/to/benchmark/results_v2

python -m benchmark score \
    --dataset /path/to/benchmark/dataset \
    --results /path/to/benchmark/results_v2 \
    --output /path/to/benchmark/report_v2.md
```
