#!/usr/bin/env python3
"""
AEI - AI Engineering Intelligence
=================================

Central orchestrator. Acts as an AI Release Manager: given a commit ID (the PR
change) it runs all three intelligence dimensions over a single benchmark run
and prints a Production Readiness Report with an APPROVE / REVIEW / BLOCK verdict.

    Quality    -> does the change still give correct answers?   (embedding sim)
    Cost       -> what does running it cost now?                (token/$ delta)
    Governance -> is it safe and compliant to ship?            (PII / policy)

The three modules are linked by two shared things:
    * the commit ID         -> Governance turns it into a diff via `git show`
    * one benchmark run     -> the model runs ONCE; the responses feed Quality
                               (accuracy), Cost (tokens) and Governance (PII leak)

Usage:
    python aei.py --commit <id>
    python aei.py --commit <id> --model-before claude-opus-4.8 --model-after claude-haiku-4-5-20251001
    python aei.py --commit <id> --json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Callable, Dict, List, Optional

from cost import evaluate_cost, evaluate_session_cost
from governance import scan_commit
from quality import load_benchmark

GeneratorFn = Callable[[str], str]

# Weights for the blended overall score (governance + quality matter most).
WEIGHTS = {"quality": 0.4, "governance": 0.4, "cost": 0.2}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _memoize(fn: GeneratorFn) -> GeneratorFn:
    """Cache a generator by input so the model runs once per unique input."""
    cache: Dict[str, str] = {}

    def wrapped(text: str) -> str:
        if text not in cache:
            cache[text] = fn(text)
        return cache[text]

    return wrapped


def _verdict(
    overall: float,
    governance: Dict[str, object],
) -> str:
    """
    Decide APPROVE / REVIEW / BLOCK. Governance has veto power: any PII present
    (prompt or response) is a compliance failure that can't be averaged away.
    """
    gov_score = governance["governance_score"]
    if governance["pii_detected"] or gov_score <= 40:
        return "BLOCK"
    if overall < 60:
        return "BLOCK"
    if overall < 80 or governance["policy_violations"] or governance["unsafe_permissions"]:
        return "REVIEW"
    return "APPROVE"


# --------------------------------------------------------------------------- #
# Core orchestration
# --------------------------------------------------------------------------- #

def generate_readiness_report(
    commit_id: str,
    benchmark: str,
    run_before: GeneratorFn,
    run_after: GeneratorFn,
    *,
    cost_mode: str = "session",
    model_before: str = "claude-sonnet-4-6",
    model_after: str = "claude-sonnet-4-6",
    budget: float = 5.0,
    repo: Optional[str] = None,
    base: Optional[str] = None,
) -> Dict[str, object]:
    """
    Run all three dimensions and assemble the Production Readiness Report.

    cost_mode:
        "session" -> dev-session cost of the commit (session_insights.py)
        "runtime" -> before/after runtime cost on the benchmark (cost_intelligence)
    """
    cases = load_benchmark(benchmark)
    inputs = [c["input"] for c in cases]

    # Run the model ONCE per input per config; share results across modules.
    before = _memoize(run_before)
    after = _memoize(run_after)
    before_responses = [before(x) for x in inputs]
    after_responses = [after(x) for x in inputs]

    # --- Quality (degrades gracefully if embedding deps aren't installed) ---
    quality: Optional[Dict[str, object]] = None
    quality_error: Optional[str] = None
    try:
        from quality import evaluate_quality

        quality = evaluate_quality(benchmark, before, after)
    except Exception as exc:  # missing sentence-transformers, model download, etc.
        quality_error = f"{type(exc).__name__}: {exc}"

    # --- Cost ---
    if cost_mode == "runtime":
        # Runtime cost shares the benchmark responses with Quality/Governance.
        cost = evaluate_cost(
            inputs,
            before_responses,
            after_responses,
            model_before=model_before,
            model_after=model_after,
        )
    else:
        # Dev-session cost links via the commit ID (session_insights.py).
        cost = evaluate_session_cost(commit_id, repo=repo, budget=budget)

    # --- Governance (commit/PR-range diff + after responses for PII-leak scan) ---
    governance = scan_commit(commit_id, after_responses, repo=repo, base=base)

    # --- Blend into an overall score (renormalised if quality is unavailable) ---
    scores = {
        "governance": governance["governance_score"],
        "cost": cost["cost_score"],
    }
    if quality is not None:
        scores["quality"] = quality["quality_score"]

    total_weight = sum(WEIGHTS[k] for k in scores)
    overall = round(sum(scores[k] * WEIGHTS[k] for k in scores) / total_weight, 2)

    verdict = _verdict(overall, governance)

    return {
        "commit": commit_id,
        "verdict": verdict,
        "overall_score": overall,
        "dimensions": {
            "quality": quality,
            "quality_error": quality_error,
            "cost": cost,
            "governance": governance,
        },
    }


# --------------------------------------------------------------------------- #
# Demo generators (used when no real AI configs are wired in)
# --------------------------------------------------------------------------- #

def _demo_generators(benchmark: str):
    """Mock before/after configs so `python aei.py` runs without real models."""
    cases = load_benchmark(benchmark)
    expected = {c["input"]: c["expected_output"] for c in cases}

    def run_before(text: str) -> str:
        return expected[text]

    def run_after(text: str) -> str:
        # Proposed config regresses on a couple of cases to make the report move.
        if "password" in text.lower():
            return "Just turn it off and on again, that usually fixes passwords."
        if "encrypt" in text.lower():
            return "Our mascot is a friendly blue otter named Pixel."
        return expected[text]

    return run_before, run_after


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

_VERDICT_BADGE = {"APPROVE": "[ APPROVE ]", "REVIEW": "[ REVIEW ]", "BLOCK": "[ BLOCK ]"}


def _print_report(report: Dict[str, object]) -> None:
    dims = report["dimensions"]
    q, c, g = dims["quality"], dims["cost"], dims["governance"]

    print("=" * 60)
    print("  AEI - PRODUCTION READINESS REPORT")
    print("=" * 60)
    print(f"  Commit:   {report['commit']}")
    print(f"  Verdict:  {_VERDICT_BADGE.get(report['verdict'], report['verdict'])}")
    print(f"  Overall:  {report['overall_score']}/100")
    print("-" * 60)

    if q is not None:
        print(f"  Quality      {q['quality_score']:>6}/100   "
              f"(acc {q['accuracy_before']} -> {q['accuracy_after']}, "
              f"hallucination delta {q['hallucination_delta']:+}pp)")
    else:
        print(f"  Quality         n/a       ({dims['quality_error']})")

    if c.get("kind") == "dev_session":
        print(f"  Cost         {c['cost_score']:>6}/100   "
              f"(dev cost ${c['dev_cost']} over {c['total_tokens']:,} tokens, "
              f"{c['num_sessions']} session(s))")
    else:
        print(f"  Cost         {c['cost_score']:>6}/100   "
              f"(${c['cost_before']} -> ${c['cost_after']}, delta {c['cost_delta_pct']:+}% )")

    print(f"  Governance   {g['governance_score']:>6}/100   "
          f"(PII {'YES' if g['pii_detected'] else 'no'}, "
          f"{len(g['policy_violations'])} policy, "
          f"{len(g['unsafe_permissions'])} perms)")
    print("-" * 60)

    if g["pii_detected"]:
        print("  ! PII detected:")
        for f in g["pii_in_prompt"]:
            print(f"      prompt:   {f}")
        for f in g["pii_in_responses"]:
            print(f"      response: {f}")
    for v in g["policy_violations"]:
        print(f"  ! policy:  {v}")
    for p in g["unsafe_permissions"]:
        print(f"  ! perms:   {p}")
    print("=" * 60)


# --------------------------------------------------------------------------- #
# Markdown report (for posting as a GitHub PR comment)
# --------------------------------------------------------------------------- #

_VERDICT_EMOJI = {"APPROVE": "✅", "REVIEW": "⚠️", "BLOCK": "⛔"}


def _score_emoji(score: object) -> str:
    if score is None:
        return "➖"
    if score >= 80:
        return "🟢"
    if score >= 60:
        return "🟡"
    return "🔴"


def render_markdown(report: Dict[str, object]) -> str:
    """Render the readiness report as Markdown suitable for a GitHub PR comment."""
    dims = report["dimensions"]
    q, c, g = dims["quality"], dims["cost"], dims["governance"]
    verdict = report["verdict"]
    lines: List[str] = []

    lines.append("## 🤖 AEI — Production Readiness Report")
    lines.append("")
    lines.append(
        f"**Verdict:** {_VERDICT_EMOJI.get(verdict, '')} **{verdict}**  ·  "
        f"**Overall score:** {report['overall_score']}/100  ·  "
        f"**Commit:** `{report['commit']}`"
    )
    lines.append("")

    # Summary table.
    lines.append("| Dimension | Score | Summary |")
    lines.append("|---|---|---|")

    if q is not None:
        lines.append(
            f"| {_score_emoji(q['quality_score'])} Quality | {q['quality_score']}/100 | "
            f"accuracy {q['accuracy_before']} → {q['accuracy_after']}, "
            f"hallucination Δ {q['hallucination_delta']:+}pp |"
        )
    else:
        lines.append(f"| ➖ Quality | n/a | not evaluated ({dims['quality_error']}) |")

    if c.get("kind") == "dev_session":
        cost_summary = (
            f"dev cost ${c['dev_cost']} over {c['total_tokens']:,} tokens "
            f"across {c['num_sessions']} session(s) (budget ${c['budget']})"
        )
    else:
        cost_summary = (
            f"${c['cost_before']} → ${c['cost_after']} ({c['cost_delta_pct']:+}%), "
            f"{c['model_before']} → {c['model_after']}"
        )
    lines.append(
        f"| {_score_emoji(c['cost_score'])} Cost | {c['cost_score']}/100 | {cost_summary} |"
    )
    lines.append(
        f"| {_score_emoji(g['governance_score'])} Governance | {g['governance_score']}/100 | "
        f"PII {'**detected**' if g['pii_detected'] else 'none'}, "
        f"{len(g['policy_violations'])} policy, {len(g['unsafe_permissions'])} perms |"
    )
    lines.append("")

    # Governance findings detail (only when there's something to show).
    findings = (
        g["pii_in_prompt"]
        or g["pii_in_responses"]
        or g["policy_violations"]
        or g["unsafe_permissions"]
    )
    if findings:
        lines.append("### Governance findings")
        for f in g["pii_in_prompt"]:
            lines.append(f"- 🔒 **PII in prompt:** {f}")
        for f in g["pii_in_responses"]:
            lines.append(f"- 🔓 **PII leaked in response:** {f}")
        for v in g["policy_violations"]:
            lines.append(f"- 📜 **Policy:** {v}")
        for p in g["unsafe_permissions"]:
            lines.append(f"- 🛠️ **Unsafe permission:** {p}")
        lines.append("")

    lines.append(
        "<sub>Generated by AEI — AI Engineering Intelligence. "
        "Quality (embedding similarity) · Cost (token/$ delta) · Governance (PII/policy).</sub>"
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python aei.py",
        description="AEI - generate a Production Readiness Report for an AI PR.",
    )
    parser.add_argument("--commit", required=True, help="Git commit-ish to evaluate.")
    parser.add_argument(
        "--benchmark",
        default="quality/benchmark.json",
        help="Path to the benchmark dataset (default: quality/benchmark.json).",
    )
    parser.add_argument(
        "--cost-mode",
        choices=["session", "runtime"],
        default="session",
        help="'session' = dev-session cost of the commit (default); "
        "'runtime' = before/after runtime cost on the benchmark.",
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=5.0,
        help="USD budget the dev-session cost is graded against (default: 5.0).",
    )
    parser.add_argument("--model-before", default="claude-sonnet-4-6",
                        help="Model id for the baseline config (runtime cost mode).")
    parser.add_argument("--model-after", default="claude-sonnet-4-6",
                        help="Model id for the proposed config (runtime cost mode).")
    parser.add_argument(
        "--base",
        default=None,
        help="Base ref for PR-range scanning (governance scans base...commit). "
        "Omit to scan a single commit.",
    )
    parser.add_argument("--repo", default=None, help="Repo path (default: cwd).")
    parser.add_argument(
        "--format",
        choices=["text", "markdown", "json"],
        default="text",
        help="Output format (default: text). 'markdown' is GitHub-PR-comment ready.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Shorthand for --format json (raw report dict).",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        default=None,
        help="Write the report to a file instead of stdout (e.g. report.md).",
    )
    args = parser.parse_args(argv)
    fmt = "json" if args.json else args.format

    # Make output UTF-8 safe on legacy Windows consoles (cp1252).
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    # Demo wiring: real AEI injects production run_before/run_after here.
    run_before, run_after = _demo_generators(args.benchmark)

    try:
        report = generate_readiness_report(
            args.commit,
            args.benchmark,
            run_before,
            run_after,
            cost_mode=args.cost_mode,
            model_before=args.model_before,
            model_after=args.model_after,
            budget=args.budget,
            repo=args.repo,
            base=args.base,
        )
    except (RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if fmt == "json":
        rendered = json.dumps(report, indent=2)
    elif fmt == "markdown":
        rendered = render_markdown(report)
    else:
        rendered = None  # text format prints directly below

    if args.out:
        text = rendered if rendered is not None else render_markdown(report)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"wrote {args.out}", file=sys.stderr)
    elif rendered is not None:
        print(rendered)
    else:
        _print_report(report)

    # Exit code mirrors the verdict so CI can gate on it.
    return {"APPROVE": 0, "REVIEW": 0, "BLOCK": 2}.get(report["verdict"], 1)


if __name__ == "__main__":
    raise SystemExit(main())
