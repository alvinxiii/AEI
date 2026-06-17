"""
AEI - Quality Intelligence Module
=================================

Evaluates the quality impact of an AI-related pull request by comparing a
"before" (baseline) AI configuration against an "after" (proposed) one on a
fixed benchmark dataset.

Method (zero extra LLM/API cost for evaluation):
    1. For each benchmark case, run the input through both the before and after
       AI configurations to collect their responses.
    2. Embed each response and its expected_output locally with
       sentence-transformers (all-MiniLM-L6-v2) -- no network calls.
    3. Score each response by cosine similarity to its expected_output.
    4. Flag a hallucination when similarity drops below a threshold (default 0.5).
    5. Aggregate into a single quality_score (0-100) and a delta vs baseline.

The module does NOT generate responses itself. Generation is injected via two
callables (`run_before` / `run_after`), keeping this module API-cost-free and
letting AEI wire in whatever real configs it needs.

Output dict (consumed by the central AEI readiness report):
    {
        "accuracy_before":    float,   # 0-100, mean similarity of baseline
        "accuracy_after":     float,   # 0-100, mean similarity of proposed
        "hallucination_delta": float,  # change in hallucination RATE (pct points)
        "consistency_score":  float,   # 0-100, stability of after-config quality
        "quality_score":      float,   # 0-100, final headline score (after)
        ... plus quality_delta and per_case detail for drill-down ...
    }
"""

from __future__ import annotations

import json
import statistics
from typing import Callable, Dict, List, Optional, Sequence, Union

from sklearn.metrics.pairwise import cosine_similarity

# Default local embedding model -- small, fast, no API cost.
DEFAULT_MODEL_NAME = "all-MiniLM-L6-v2"

# Below this cosine similarity, a response is flagged as a likely hallucination.
DEFAULT_HALLUCINATION_THRESHOLD = 0.5

GeneratorFn = Callable[[str], str]
Benchmark = Union[str, Sequence[Dict[str, str]]]


# --------------------------------------------------------------------------- #
# Embedding helper (lazy-loaded so importing this module is cheap)
# --------------------------------------------------------------------------- #

_MODEL_CACHE: Dict[str, object] = {}


def _get_model(model_name: str):
    """Load (and cache) a sentence-transformers model by name."""
    if model_name not in _MODEL_CACHE:
        # Imported lazily so the module can be imported without the heavy dep
        # present (e.g. for structural tests or when only loading benchmarks).
        from sentence_transformers import SentenceTransformer

        _MODEL_CACHE[model_name] = SentenceTransformer(model_name)
    return _MODEL_CACHE[model_name]


def _embed(texts: Sequence[str], model_name: str):
    """Embed a list of texts into a 2D numpy array of vectors."""
    model = _get_model(model_name)
    return model.encode(list(texts), convert_to_numpy=True, show_progress_bar=False)


# --------------------------------------------------------------------------- #
# Benchmark loading / validation
# --------------------------------------------------------------------------- #

def load_benchmark(benchmark: Benchmark) -> List[Dict[str, str]]:
    """
    Accept either a path to a JSON file or an already-loaded list of cases,
    and return a validated list of {input, expected_output, [id]} dicts.
    """
    if isinstance(benchmark, str):
        with open(benchmark, "r", encoding="utf-8") as fh:
            cases = json.load(fh)
    else:
        cases = list(benchmark)

    if not isinstance(cases, list) or not cases:
        raise ValueError("Benchmark must be a non-empty array of test cases.")

    validated: List[Dict[str, str]] = []
    for i, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"Benchmark case {i} is not an object.")
        if "input" not in case or "expected_output" not in case:
            raise ValueError(
                f"Benchmark case {i} is missing 'input' or 'expected_output'."
            )
        validated.append(
            {
                "id": str(case.get("id", f"case-{i + 1:03d}")),
                "input": str(case["input"]),
                "expected_output": str(case["expected_output"]),
            }
        )
    return validated


# --------------------------------------------------------------------------- #
# Core scoring math
# --------------------------------------------------------------------------- #

def _pairwise_cosine(a, b) -> List[float]:
    """Cosine similarity for aligned rows of `a` and `b` -> list of floats."""
    # Full matrix then take the diagonal: cheap at benchmark scale and avoids a
    # Python loop over rows.
    sim_matrix = cosine_similarity(a, b)
    return [float(sim_matrix[i][i]) for i in range(len(sim_matrix))]


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _to_100(x: float) -> float:
    """Map a cosine score (roughly -1..1, usually 0..1) to a 0-100 scale."""
    return round(_clamp01(x) * 100.0, 2)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def evaluate_quality(
    benchmark: Benchmark,
    run_before: GeneratorFn,
    run_after: GeneratorFn,
    *,
    model_name: str = DEFAULT_MODEL_NAME,
    hallucination_threshold: float = DEFAULT_HALLUCINATION_THRESHOLD,
) -> Dict[str, object]:
    """
    Evaluate the quality delta between two AI configurations on a benchmark.

    Args:
        benchmark: Path to a benchmark JSON file, or a list of cases. Each case
            needs `input` and `expected_output` (optional `id`).
        run_before: Callable mapping an input string -> the baseline config's
            response string.
        run_after: Callable mapping an input string -> the proposed config's
            response string.
        model_name: sentence-transformers model to embed with (local, free).
        hallucination_threshold: cosine similarity below which a response is
            flagged as a likely hallucination.

    Returns:
        A dict ready to feed into the AEI readiness report (see module docstring).
    """
    cases = load_benchmark(benchmark)
    n = len(cases)

    # 1. Collect responses from both configurations.
    before_responses = [run_before(c["input"]) for c in cases]
    after_responses = [run_after(c["input"]) for c in cases]
    expected = [c["expected_output"] for c in cases]

    # 2. Embed everything in a single batched pass (one encode call per group).
    exp_vecs = _embed(expected, model_name)
    before_vecs = _embed(before_responses, model_name)
    after_vecs = _embed(after_responses, model_name)

    # 3. Per-case cosine similarity vs the expected output.
    before_sims = _pairwise_cosine(before_vecs, exp_vecs)
    after_sims = _pairwise_cosine(after_vecs, exp_vecs)

    # 4. Per-case detail + hallucination flags.
    per_case = []
    before_hallucinations = 0
    after_hallucinations = 0
    for i, c in enumerate(cases):
        b_sim = _clamp01(before_sims[i])
        a_sim = _clamp01(after_sims[i])
        b_flag = b_sim < hallucination_threshold
        a_flag = a_sim < hallucination_threshold
        before_hallucinations += int(b_flag)
        after_hallucinations += int(a_flag)
        per_case.append(
            {
                "id": c["id"],
                "input": c["input"],
                "score_before": _to_100(b_sim),
                "score_after": _to_100(a_sim),
                "delta": round((a_sim - b_sim) * 100.0, 2),
                "hallucination_before": b_flag,
                "hallucination_after": a_flag,
                "regressed": a_sim < b_sim,
            }
        )

    # 5. Aggregate scores.
    accuracy_before = _to_100(statistics.fmean(before_sims))
    accuracy_after = _to_100(statistics.fmean(after_sims))

    before_hall_rate = before_hallucinations / n
    after_hall_rate = after_hallucinations / n
    # Positive delta == hallucinations got WORSE after the change.
    hallucination_delta = round((after_hall_rate - before_hall_rate) * 100.0, 2)

    # Consistency: how tightly clustered the after-config scores are. A config
    # that is uniformly decent is more trustworthy to ship than one that is
    # excellent on some cases and broken on others. std of 0 -> 100.
    if n > 1:
        after_std = statistics.pstdev(after_sims)  # 0..~0.5 in practice
        consistency_score = round(_clamp01(1.0 - 2.0 * after_std) * 100.0, 2)
    else:
        consistency_score = 100.0

    # Headline quality score = after accuracy, penalised for any net increase in
    # hallucinations. Keeps a single number the release report can gate on.
    penalty = max(0.0, hallucination_delta)  # only penalise regressions
    quality_score = round(max(0.0, accuracy_after - penalty), 2)
    quality_delta = round(accuracy_after - accuracy_before, 2)

    return {
        "accuracy_before": accuracy_before,
        "accuracy_after": accuracy_after,
        "hallucination_delta": hallucination_delta,
        "consistency_score": consistency_score,
        "quality_score": quality_score,
        # ---- extra context for the report / debugging (not required fields) ----
        "quality_delta": quality_delta,
        "hallucination_count_before": before_hallucinations,
        "hallucination_count_after": after_hallucinations,
        "num_cases": n,
        "hallucination_threshold": hallucination_threshold,
        "model_name": model_name,
        "per_case": per_case,
    }


# --------------------------------------------------------------------------- #
# Demo / smoke test
# --------------------------------------------------------------------------- #

def _demo() -> None:
    """
    Run a self-contained demo against the bundled benchmark.json using mock
    before/after generators, so you can see the output shape without wiring in
    real AI configs.
    """
    import os

    benchmark_path = os.path.join(os.path.dirname(__file__), "benchmark.json")
    cases = load_benchmark(benchmark_path)
    expected_by_input = {c["input"]: c["expected_output"] for c in cases}

    def run_before(text: str) -> str:
        # Baseline config: returns the correct answer (high quality).
        return expected_by_input[text]

    def run_after(text: str) -> str:
        # Proposed config: regresses on a couple of cases to show the deltas.
        if "password" in text.lower():
            return "Just turn it off and on again, that usually fixes passwords."
        if "encrypt" in text.lower():
            return "Our mascot is a friendly blue otter named Pixel."
        return expected_by_input[text]

    result = evaluate_quality(benchmark_path, run_before, run_after)

    print(json.dumps({k: v for k, v in result.items() if k != "per_case"}, indent=2))
    print("\nPer-case:")
    for row in result["per_case"]:
        flag = "  HALLUCINATION" if row["hallucination_after"] else ""
        print(
            f"  {row['id']}: {row['score_before']:>6} -> {row['score_after']:>6}"
            f"  (Δ {row['delta']:+.2f}){flag}"
        )


if __name__ == "__main__":
    _demo()
