"""Scoring module: compare model extraction output against human-verified ground truth.

Provides field-level and overall accuracy metrics for benchmarking model quality.

Scoring strategy per field type:
- String fields: case-insensitive exact match (after stripping whitespace)
- Numeric fields: exact match for integers; within tolerance for floats
- Boolean fields: exact match
- List fields (e.g., motors, rocket_colors): order-insensitive element matching
- Nested objects (e.g., membership, measurements): recursive field comparison
- None handling: both None = match; one None and one not = mismatch

Each field comparison returns a score between 0.0 and 1.0.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Field comparison functions
# ---------------------------------------------------------------------------

FLOAT_TOLERANCE = 0.01  # Allow 1% relative tolerance for float comparisons


def _normalize_string(value: Any) -> str | None:
    """Normalize a string value for comparison."""
    if value is None:
        return None
    s = str(value).strip().lower()
    return s if s else None


def _compare_strings(expected: Any, actual: Any) -> float:
    """Compare two string values, case-insensitive. Returns 0.0 or 1.0."""
    e = _normalize_string(expected)
    a = _normalize_string(actual)
    if e is None and a is None:
        return 1.0
    if e is None or a is None:
        return 0.0
    return 1.0 if e == a else 0.0


def _compare_strings_fuzzy(expected: Any, actual: Any) -> float:
    """Compare two strings with partial credit for substring containment."""
    e = _normalize_string(expected)
    a = _normalize_string(actual)
    if e is None and a is None:
        return 1.0
    if e is None or a is None:
        return 0.0
    if e == a:
        return 1.0
    # Partial credit: one contains the other
    if e in a or a in e:
        return 0.75
    return 0.0


def _compare_numbers(expected: Any, actual: Any, is_integer: bool = False) -> float:
    """Compare two numeric values. Returns 0.0 or 1.0."""
    if expected is None and actual is None:
        return 1.0
    if expected is None or actual is None:
        return 0.0
    try:
        e = float(expected)
        a = float(actual)
    except (ValueError, TypeError):
        return 0.0

    if is_integer:
        return 1.0 if int(e) == int(a) else 0.0

    # Float comparison with relative tolerance
    if e == 0:
        return 1.0 if abs(a) < FLOAT_TOLERANCE else 0.0
    relative_diff = abs(e - a) / abs(e)
    return 1.0 if relative_diff <= FLOAT_TOLERANCE else 0.0


def _compare_booleans(expected: Any, actual: Any) -> float:
    """Compare two boolean values. Returns 0.0 or 1.0."""
    if expected is None and actual is None:
        return 1.0
    if expected is None or actual is None:
        return 0.0
    return 1.0 if bool(expected) == bool(actual) else 0.0


def _compare_motor(expected: dict, actual: dict) -> float:
    """Compare a single motor entry. Weighted scoring of components."""
    if not expected or not actual:
        return 0.0 if (expected or actual) else 1.0

    scores = []
    weights = []

    # Letter is critical (impulse class)
    scores.append(_compare_strings(expected.get("letter"), actual.get("letter")))
    weights.append(3.0)

    # Number (average thrust) is important
    scores.append(_compare_strings(expected.get("number"), actual.get("number")))
    weights.append(2.0)

    # Manufacturer
    scores.append(_compare_strings(expected.get("manufacturer"), actual.get("manufacturer")))
    weights.append(1.0)

    # Suffix
    scores.append(_compare_strings(expected.get("suffix"), actual.get("suffix")))
    weights.append(1.0)

    # Leading number (rare, low weight)
    scores.append(_compare_strings(expected.get("leading_number"), actual.get("leading_number")))
    weights.append(0.5)

    # Quantity
    e_qty = expected.get("quantity", 1)
    a_qty = actual.get("quantity", 1)
    scores.append(1.0 if e_qty == a_qty else 0.0)
    weights.append(1.0)

    total_weight = sum(weights)
    return sum(s * w for s, w in zip(scores, weights)) / total_weight


def _compare_motors(expected: Any, actual: Any) -> float:
    """Compare motor lists. Order-sensitive since motor order matters for staging."""
    if expected is None and actual is None:
        return 1.0
    if expected is None or actual is None:
        return 0.0
    if not isinstance(expected, list) or not isinstance(actual, list):
        return 0.0

    if len(expected) == 0 and len(actual) == 0:
        return 1.0
    if len(expected) == 0 or len(actual) == 0:
        return 0.0

    # Compare up to the length of the expected list
    max_len = max(len(expected), len(actual))
    total_score = 0.0

    for i in range(max_len):
        if i < len(expected) and i < len(actual):
            total_score += _compare_motor(expected[i], actual[i])
        # else: missing or extra motor = 0 for that slot

    return total_score / max_len


def _compare_measurements(expected: Any, actual: Any) -> float:
    """Compare measurement objects (diameter, length, weight with units)."""
    if expected is None and actual is None:
        return 1.0
    if expected is None or actual is None:
        return 0.0
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return 0.0

    scores = []
    for dim in ("diameter", "length", "weight"):
        e_val = expected.get(dim)
        a_val = actual.get(dim)
        e_unit = expected.get(f"{dim}_unit")
        a_unit = actual.get(f"{dim}_unit")

        # Skip dimensions where both are None
        if e_val is None and a_val is None:
            continue

        val_score = _compare_numbers(e_val, a_val)
        unit_score = _compare_strings(e_unit, a_unit)
        # Weight value match more than unit
        scores.append(val_score * 0.7 + unit_score * 0.3)

    return sum(scores) / len(scores) if scores else 1.0


def _compare_membership(expected: Any, actual: Any) -> float:
    """Compare membership objects."""
    if expected is None and actual is None:
        return 1.0
    if expected is None or actual is None:
        return 0.0
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return 0.0

    scores = []
    # Club
    scores.append(_compare_strings(expected.get("club"), actual.get("club")))
    # Member number
    e_num = _normalize_string(expected.get("member_number"))
    a_num = _normalize_string(actual.get("member_number"))
    if e_num and a_num:
        # Strip leading zeros for comparison
        e_num = e_num.lstrip("0") or "0"
        a_num = a_num.lstrip("0") or "0"
        scores.append(1.0 if e_num == a_num else 0.0)
    elif e_num is None and a_num is None:
        scores.append(1.0)
    else:
        scores.append(0.0)
    # Cert level
    scores.append(_compare_numbers(expected.get("cert_level"), actual.get("cert_level"), is_integer=True))

    return sum(scores) / len(scores) if scores else 1.0


def _compare_string_list(expected: Any, actual: Any) -> float:
    """Compare lists of strings (e.g., rocket_colors). Order-insensitive."""
    if expected is None and actual is None:
        return 1.0
    if expected is None or actual is None:
        return 0.0
    if not isinstance(expected, list) or not isinstance(actual, list):
        return 0.0

    e_set = {_normalize_string(s) for s in expected if s} - {None}
    a_set = {_normalize_string(s) for s in actual if s} - {None}

    if not e_set and not a_set:
        return 1.0
    if not e_set or not a_set:
        return 0.0

    # Jaccard similarity
    intersection = e_set & a_set
    union = e_set | a_set
    return len(intersection) / len(union) if union else 1.0


# ---------------------------------------------------------------------------
# Field definitions with comparison strategy and importance weight
# ---------------------------------------------------------------------------

@dataclass
class FieldSpec:
    """Specification for how to compare a single field."""
    name: str
    compare_fn: Any  # Callable[[Any, Any], float]
    weight: float = 1.0
    category: str = "general"


# Define all fields to compare with their strategies and weights.
# Higher weight = more important for overall accuracy score.
FIELD_SPECS: list[FieldSpec] = [
    # High importance: identity and critical fields
    FieldSpec("flier_name", _compare_strings_fuzzy, weight=3.0, category="identity"),
    FieldSpec("flight_date", _compare_strings, weight=2.0, category="identity"),
    FieldSpec("motors", _compare_motors, weight=3.0, category="technical"),

    # Medium importance: operational fields
    FieldSpec("evaluation_outcome", _compare_strings, weight=2.0, category="evaluation"),
    FieldSpec("rack", _compare_strings, weight=1.5, category="operational"),
    FieldSpec("pad", lambda e, a: _compare_numbers(e, a, is_integer=True), weight=1.5, category="operational"),
    FieldSpec("fso_rso_initials", _compare_strings, weight=1.5, category="operational"),
    FieldSpec("recovery_plan", _compare_strings_fuzzy, weight=1.5, category="technical"),

    # Boolean flags
    FieldSpec("flag_heads_up", _compare_booleans, weight=1.0, category="flags"),
    FieldSpec("flag_first_flight", _compare_booleans, weight=1.0, category="flags"),
    FieldSpec("flag_complex", _compare_booleans, weight=1.0, category="flags"),

    # Supplementary fields
    FieldSpec("membership", _compare_membership, weight=1.5, category="identity"),
    FieldSpec("measurements", _compare_measurements, weight=1.0, category="technical"),
    FieldSpec("total_impulse_value", _compare_numbers, weight=1.0, category="technical"),
    FieldSpec("total_impulse_unit", _compare_strings, weight=0.5, category="technical"),
    FieldSpec("rocket_name", _compare_strings_fuzzy, weight=1.0, category="rocket"),
    FieldSpec("rocket_manufacturer", _compare_strings_fuzzy, weight=0.5, category="rocket"),
    FieldSpec("rocket_colors", _compare_string_list, weight=0.5, category="rocket"),
    FieldSpec("evaluation_comments", _compare_strings_fuzzy, weight=0.5, category="evaluation"),
    FieldSpec("notes", _compare_strings_fuzzy, weight=0.5, category="general"),
]


# ---------------------------------------------------------------------------
# Scoring results
# ---------------------------------------------------------------------------

@dataclass
class FieldResult:
    """Result of comparing a single field for a single sample."""
    field_name: str
    score: float
    weight: float
    category: str
    expected: Any = None
    actual: Any = None


@dataclass
class SampleResult:
    """Result of scoring a single sample (one image, one model)."""
    record_id: int
    field_results: list[FieldResult] = field(default_factory=list)
    weighted_score: float = 0.0
    unweighted_score: float = 0.0
    extraction_success: bool = True

    def compute_scores(self) -> None:
        """Compute weighted and unweighted aggregate scores."""
        if not self.field_results:
            self.weighted_score = 0.0
            self.unweighted_score = 0.0
            return

        total_weight = sum(fr.weight for fr in self.field_results)
        self.weighted_score = (
            sum(fr.score * fr.weight for fr in self.field_results) / total_weight
            if total_weight > 0 else 0.0
        )
        self.unweighted_score = (
            sum(fr.score for fr in self.field_results) / len(self.field_results)
        )


@dataclass
class ModelScorecard:
    """Aggregate scores for a single model across all samples."""
    model_name: str
    sample_results: list[SampleResult] = field(default_factory=list)
    overall_weighted: float = 0.0
    overall_unweighted: float = 0.0
    field_averages: dict[str, float] = field(default_factory=dict)
    category_averages: dict[str, float] = field(default_factory=dict)
    extraction_success_rate: float = 0.0

    def compute_aggregates(self) -> None:
        """Compute aggregate statistics from sample results."""
        if not self.sample_results:
            return

        # Overall scores (across all samples)
        successful = [s for s in self.sample_results if s.extraction_success]
        self.extraction_success_rate = len(successful) / len(self.sample_results)

        if not successful:
            return

        self.overall_weighted = sum(s.weighted_score for s in successful) / len(successful)
        self.overall_unweighted = sum(s.unweighted_score for s in successful) / len(successful)

        # Per-field averages
        field_scores: dict[str, list[float]] = {}
        category_scores: dict[str, list[float]] = {}

        for sample in successful:
            for fr in sample.field_results:
                field_scores.setdefault(fr.field_name, []).append(fr.score)
                category_scores.setdefault(fr.category, []).append(fr.score)

        self.field_averages = {
            name: sum(scores) / len(scores)
            for name, scores in field_scores.items()
        }
        self.category_averages = {
            cat: sum(scores) / len(scores)
            for cat, scores in category_scores.items()
        }


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def score_sample(
    record_id: int,
    ground_truth: dict,
    model_output: dict | None,
) -> SampleResult:
    """Score a single sample by comparing model output against ground truth.

    Args:
        record_id: The record ID for this sample.
        ground_truth: The verified ground truth dict.
        model_output: The raw model extraction output dict, or None if extraction failed.

    Returns:
        A SampleResult with per-field scores.
    """
    result = SampleResult(record_id=record_id)

    if model_output is None:
        result.extraction_success = False
        # Score all fields as 0
        for spec in FIELD_SPECS:
            result.field_results.append(FieldResult(
                field_name=spec.name,
                score=0.0,
                weight=spec.weight,
                category=spec.category,
                expected=ground_truth.get(spec.name),
                actual=None,
            ))
        result.compute_scores()
        return result

    for spec in FIELD_SPECS:
        expected = ground_truth.get(spec.name)
        actual = model_output.get(spec.name)

        score = spec.compare_fn(expected, actual)

        result.field_results.append(FieldResult(
            field_name=spec.name,
            score=score,
            weight=spec.weight,
            category=spec.category,
            expected=expected,
            actual=actual,
        ))

    result.compute_scores()
    return result


def score_model(
    model_name: str,
    dataset_dir: Path,
    results_dir: Path,
) -> ModelScorecard:
    """Score all samples for a given model.

    Args:
        model_name: The model name (used to find raw outputs).
        dataset_dir: Path to the benchmark dataset directory.
        results_dir: Path to the benchmark results directory.

    Returns:
        A ModelScorecard with aggregate statistics.
    """
    manifest = json.loads((dataset_dir / "manifest.json").read_text())
    model_dir_name = model_name.replace("/", "_").replace(":", "_")
    model_raw_dir = results_dir / "raw_outputs" / model_dir_name

    scorecard = ModelScorecard(model_name=model_name)

    for sample in manifest:
        record_id = sample["record_id"]
        gt_path = dataset_dir / sample["ground_truth_file"]
        output_path = model_raw_dir / f"{record_id}.json"

        ground_truth = json.loads(gt_path.read_text())

        model_output = None
        if output_path.exists():
            raw = json.loads(output_path.read_text())
            # Check if it's an error record
            if "error" not in raw:
                model_output = raw

        sample_result = score_sample(record_id, ground_truth, model_output)
        scorecard.sample_results.append(sample_result)

    scorecard.compute_aggregates()
    return scorecard


def score_all_models(
    dataset_dir: Path,
    results_dir: Path,
) -> list[ModelScorecard]:
    """Score all models found in the results directory.

    Reads run_metadata.json to discover which models were benchmarked.

    Returns:
        List of ModelScorecards, sorted by overall weighted score (descending).
    """
    metadata_path = results_dir / "run_metadata.json"
    if not metadata_path.exists():
        raise FileNotFoundError(f"run_metadata.json not found in {results_dir}")

    metadata = json.loads(metadata_path.read_text())
    models = metadata.get("models", [])

    scorecards = []
    for model in models:
        scorecard = score_model(model, dataset_dir, results_dir)
        scorecards.append(scorecard)

    # Sort by overall weighted score, descending
    scorecards.sort(key=lambda sc: sc.overall_weighted, reverse=True)
    return scorecards
