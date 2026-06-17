"""
AEI - Cost Intelligence (dev-session mode)
==========================================

Reports the *development* cost of a PR by tying the commit ID to the local AI
coding session(s) that produced it, via session/session_insights.py's `--pr`
matcher. Answers "how much did it cost to build this change?".

This is the cost dimension the AEI readiness report surfaces. It links to the
same commit ID the other dimensions use -- session_insights resolves the
commit's changed files and finds the Claude Code / Copilot session(s) that
touched them, then sums their token usage and estimated cost.

No API calls: session_insights reads local session files and prices them from
session/modelPricing.json.

Output dict (consumed by the central AEI readiness report):
    {
        "kind":         "dev_session",
        "dev_cost":     float,   # total $ across matched sessions
        "total_tokens": int,
        "num_sessions": int,
        "sessions":     [ {title, cost, tokens, models}, ... ],
        "budget":       float,   # $ budget the score is graded against
        "cost_score":   float,   # 0-100 (100 = within budget)
        "source":       str,     # e.g. "commit 695412b"
        "commit":       str,
    }
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Dict, List, Optional

# Repo root = the AEI project directory (parent of this `cost/` package).
_REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
_SCRIPT_PATH = os.path.join(_REPO_ROOT, "session", "session_insights.py")

# Default budget (USD) a single change's dev cost is graded against.
DEFAULT_BUDGET = 5.0


def _run_session_insights(commit_id: str, repo: Optional[str]) -> Dict:
    """Call session_insights.py --pr <commit> --json and parse the result."""
    cmd = [sys.executable, _SCRIPT_PATH, "--pr", commit_id, "--json"]
    try:
        proc = subprocess.run(
            cmd,
            # session_insights uses its CWD as the repo root, so run it from the
            # repo root (not the session/ subdir) for session matching to work.
            cwd=repo or _REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise RuntimeError("python executable not found to run session_insights") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"session_insights --pr {commit_id!r} failed: "
            f"{proc.stderr.strip() or 'unknown error'}"
        )
    if not proc.stdout.strip():
        return {"source": f"commit {commit_id}", "matches": []}
    return json.loads(proc.stdout)


def evaluate_session_cost(
    commit_id: str,
    repo: Optional[str] = None,
    *,
    budget: float = DEFAULT_BUDGET,
) -> Dict[str, object]:
    """
    Resolve the dev-session cost of the commit and grade it against a budget.

    Args:
        commit_id: The commit AEI is evaluating.
        repo: Repo path to run session_insights from (default: the AEI repo).
        budget: USD budget; cost at/under budget scores 100, scaling down above.

    Returns:
        A cost dict ready to feed into the AEI readiness report.
    """
    data = _run_session_insights(commit_id, repo)
    matches: List[Dict] = data.get("matches", []) or []

    sessions: List[Dict[str, object]] = []
    dev_cost = 0.0
    total_tokens = 0
    for m in matches:
        cost = float(m.get("estimated_cost") or 0.0)
        tokens = int(m.get("total_tokens") or 0)
        dev_cost += cost
        total_tokens += tokens
        sessions.append(
            {
                "title": m.get("title"),
                "cost": round(cost, 6),
                "tokens": tokens,
                "models": m.get("models", []),
            }
        )

    dev_cost = round(dev_cost, 6)

    # Score: within budget -> 100; every 1% over budget costs 1 point.
    if budget > 0 and dev_cost > budget:
        over_pct = (dev_cost - budget) / budget * 100.0
        cost_score = round(max(0.0, 100.0 - over_pct), 2)
    else:
        cost_score = 100.0

    return {
        "kind": "dev_session",
        "dev_cost": dev_cost,
        "total_tokens": total_tokens,
        "num_sessions": len(sessions),
        "sessions": sessions,
        "budget": budget,
        "cost_score": cost_score,
        "source": data.get("source", f"commit {commit_id}"),
        "commit": commit_id,
    }
