"""
AEI - Cost Intelligence Module
==============================

Estimates the operational cost impact of an AI-related pull request by comparing
the token usage and dollar cost of the BEFORE (baseline) vs AFTER (proposed) AI
configuration on the same benchmark run that Quality and Governance use.

This is the *runtime* cost of the change -- "if we ship this prompt/model swap,
what does each request cost now?" -- which is the operationally meaningful number
for a release gate. (It is distinct from session/session_insights.py, which
measures local dev-session cost.)

No API calls: token counts are estimated locally (chars-per-token heuristic) and
priced from session/modelPricing.json.

Output dict (consumed by the central AEI readiness report):
    {
        "cost_before":   float,   # total $ for the baseline config over the benchmark
        "cost_after":    float,   # total $ for the proposed config
        "cost_delta":    float,   # cost_after - cost_before  (+ = more expensive)
        "cost_delta_pct": float,  # percentage change vs baseline
        "tokens_before": int,
        "tokens_after":  int,
        "cost_score":    float,   # 0-100 (100 = same-or-cheaper, lower = pricier)
        "model_before":  str,
        "model_after":   str,
    }
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Sequence

# Rough average of ~4 characters per token; good enough for a relative delta.
CHARS_PER_TOKEN = 4.0

_PRICING_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "session", "modelPricing.json"
)


# --------------------------------------------------------------------------- #
# Pricing
# --------------------------------------------------------------------------- #

_PRICING_CACHE: Optional[Dict[str, Dict]] = None


def load_pricing(path: str = _PRICING_PATH) -> Dict[str, Dict]:
    """Load and cache the model -> pricing map from modelPricing.json."""
    global _PRICING_CACHE
    if _PRICING_CACHE is None:
        with open(path, "r", encoding="utf-8") as fh:
            _PRICING_CACHE = json.load(fh)["pricing"]
    return _PRICING_CACHE


def _rates(model: str, pricing: Dict[str, Dict]) -> Dict[str, float]:
    """Return {input, output} $-per-million-token rates for a model."""
    entry = pricing.get(model)
    if entry is None:
        # Unknown model: fall back to a mid-range default so the run still works.
        return {"input": 1.0, "output": 3.0}
    return {
        "input": float(entry.get("inputCostPerMillion", 0.0)),
        "output": float(entry.get("outputCostPerMillion", 0.0)),
    }


# --------------------------------------------------------------------------- #
# Token estimation
# --------------------------------------------------------------------------- #

def estimate_tokens(text: str) -> int:
    """Estimate token count from character length."""
    if not text:
        return 0
    return max(1, round(len(text) / CHARS_PER_TOKEN))


def _config_cost(
    inputs: Sequence[str],
    outputs: Sequence[str],
    model: str,
    pricing: Dict[str, Dict],
) -> Dict[str, float]:
    """Total input/output tokens and $ cost for one config over the benchmark."""
    rates = _rates(model, pricing)
    in_tokens = sum(estimate_tokens(t) for t in inputs)
    out_tokens = sum(estimate_tokens(t) for t in outputs)
    cost = (
        in_tokens * rates["input"] / 1_000_000.0
        + out_tokens * rates["output"] / 1_000_000.0
    )
    return {
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "total_tokens": in_tokens + out_tokens,
        "cost": cost,
    }


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def evaluate_cost(
    inputs: Sequence[str],
    before_responses: Sequence[str],
    after_responses: Sequence[str],
    *,
    model_before: str = "claude-sonnet-4-6",
    model_after: str = "claude-sonnet-4-6",
    pricing_path: str = _PRICING_PATH,
) -> Dict[str, object]:
    """
    Estimate the cost delta between two AI configurations on a benchmark run.

    Args:
        inputs: The benchmark input strings (shared by both configs).
        before_responses: Baseline config responses (aligned with `inputs`).
        after_responses: Proposed config responses (aligned with `inputs`).
        model_before / model_after: Model ids used to price each config. A
            model swap (e.g. opus -> haiku) shows up here as a cost change even
            if token counts are similar.
        pricing_path: Path to modelPricing.json.

    Returns:
        A cost dict ready to feed into the AEI readiness report.
    """
    pricing = load_pricing(pricing_path)

    before = _config_cost(inputs, before_responses, model_before, pricing)
    after = _config_cost(inputs, after_responses, model_after, pricing)

    cost_before = round(before["cost"], 6)
    cost_after = round(after["cost"], 6)
    cost_delta = round(cost_after - cost_before, 6)
    cost_delta_pct = (
        round((cost_delta / cost_before) * 100.0, 2) if cost_before > 0 else 0.0
    )

    # Score: same-or-cheaper -> 100; every 1% more expensive costs 1 point.
    cost_score = round(max(0.0, min(100.0, 100.0 - max(0.0, cost_delta_pct))), 2)

    return {
        "cost_before": cost_before,
        "cost_after": cost_after,
        "cost_delta": cost_delta,
        "cost_delta_pct": cost_delta_pct,
        "tokens_before": before["total_tokens"],
        "tokens_after": after["total_tokens"],
        "cost_score": cost_score,
        "model_before": model_before,
        "model_after": model_after,
    }
