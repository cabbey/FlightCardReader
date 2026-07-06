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
    --output benchmark/dataset

# 2. Run the benchmark against multiple models
python -m benchmark run \
    --dataset benchmark/dataset \
    --models qwen3-vl gemma3:27b minicpm-v \
    --endpoint http://localhost:11434 \
    --output benchmark/results

# 3. Score results and generate the comparison report
python -m benchmark score \
    --dataset benchmark/dataset \
    --results benchmark/results \
    --output benchmark/report.md
```

## Detailed Usage

### Step 1: Export Dataset

```bash
python -m benchmark export \
    --db <path-to-flight_cards.db> \
    --image-dir <path-to-images-directory> \
    --output <output-directory>
```

**Arguments:**
| Argument | Required | Description |
|----------|----------|-------------|
| `--db` | Yes | Path to the SQLite database file |
| `--image-dir` | Yes | Path to the image store directory (parent of `image_path` values in DB) |
| `--output` | No | Output directory (default: `benchmark/dataset`) |

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
    [--event-start "April 24, 2026"] \
    [--event-end "April 26, 2026"] \
    [--samples N]
```

**Arguments:**
| Argument | Required | Description |
|----------|----------|-------------|
| `--dataset` | No | Path to exported dataset (default: `benchmark/dataset`) |
| `--models` | Yes | Space-separated list of Ollama model names |
| `--endpoint` | No | Ollama API URL (default: `http://localhost:11434`) |
| `--output` | No | Results output directory (default: `benchmark/results`) |
| `--event-start` | No | Event start date for the prompt context |
| `--event-end` | No | Event end date for the prompt context |
| `--samples` | No | Limit to N samples (useful for quick testing) |

**Output structure:**
```
results/
├── run_metadata.json      # Run configuration, timing summaries
├── raw_outputs/           # Per-model raw extraction JSON
│   ├── qwen3-vl/
│   │   ├── 1.json
│   │   └── ...
│   ├── gemma3_27b/
│   │   └── ...
│   └── ...
└── timings/               # Per-model timing data
    ├── qwen3-vl.json
    ├── gemma3_27b.json
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
| `--dataset` | No | Path to benchmark dataset (default: `benchmark/dataset`) |
| `--results` | No | Path to benchmark results (default: `benchmark/results`) |
| `--output` | No | Markdown report output path (default: `benchmark/report.md`) |
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
| Motors | Weighted component comparison (letter > number > manufacturer > suffix) |
| Measurements | Per-dimension value + unit comparison |
| Membership | Club + member number + cert level comparison |
| String lists | Jaccard similarity (order-insensitive) |

### Weighted vs Unweighted Accuracy

- **Weighted accuracy** prioritizes critical fields that matter most for record keeping:
  - High weight (3x): `flier_name`, `motors`
  - Medium weight (2x): `flight_date`, `evaluation_outcome`
  - Standard weight (1.5x): `rack`, `pad`, `fso_rso_initials`, `recovery_plan`, `membership`
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
    --image-dir /home/user/events/2026/march/images

# Quick test with 3 samples
python -m benchmark run \
    --models qwen3-vl gemma3:27b minicpm-v \
    --samples 3

# Full benchmark (may take a while depending on hardware and sample count)
python -m benchmark run \
    --models qwen3-vl gemma3:27b minicpm-v

# Generate comparison report
python -m benchmark score --json-output benchmark/report.json
```

## Adding the Dataset to Version Control

The exported dataset directory (`benchmark/dataset/`) contains images and can be large.
Consider:
- Adding `benchmark/dataset/` to `.gitignore` if images are sensitive or large
- Using Git LFS for the images directory
- Keeping just the `manifest.json` and `ground_truth/` in version control

The `benchmark/results/` directory should generally be in `.gitignore` since results
are machine-specific.

## Re-running After Prompt Changes

If you modify the extraction prompt in the main application, you can re-run the benchmark
to see how the change affects each model:

```bash
# Results are saved per-run, so use a different output directory to compare
python -m benchmark run \
    --models qwen3-vl \
    --output benchmark/results_v2

python -m benchmark score \
    --results benchmark/results_v2 \
    --output benchmark/report_v2.md
```
