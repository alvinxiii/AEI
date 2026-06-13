#!/usr/bin/env python3
"""Session Insights — a standalone Python replica of the "Today's Sessions" table
shown in the AI Engineering Fluency VS Code extension (vscode-extension/src/webview/usage/main.ts),
plus a --pr mode that pinpoints which session produced a given PR/commit/branch.

It reads the same local session sources the extension reads and prints an individual
per-session breakdown for today, sorted by number of interactions (most active first):

  * Claude Code   — ~/.claude/projects/**/*.jsonl
                    Actual Anthropic API token counts (no estimation), per
                    src/claudecode.ts + src/adapters/claudeCodeAdapter.ts.
  * Copilot Chat  — VS Code workspaceStorage/<hash>/{chatSessions,GitHub.copilot*/...}
                    Delta-based JSONL (kind:0/1/2) reconstructed per src/sessionParser.ts;
                    Input/Output use ACTUAL result.metadata.{promptTokens,outputTokens}
                    when present, falling back to char×ratio estimates
                    (src/tokenEstimation.ts). Thinking is always estimated.

Cost is computed from modelPricing.json with the same maths as
src/tokenEstimation.ts:calculateEstimatedCost.

Stdlib only — no third-party packages required. Python 3.8+.

Usage:
    python session_insights.py                # today's sessions
    python session_insights.py --date 2026-06-10
    python session_insights.py --all          # every session, regardless of date
    python session_insights.py --days 7       # sessions active in the last N days
    python session_insights.py --json         # machine-readable JSON output
    python session_insights.py --sort totalTokens   # sort by another column
    python session_insights.py --pr           # which session produced the current branch's PR
    python session_insights.py --pr 1401      # ...the GitHub PR #1401 (resolved via gh)
    python session_insights.py --pr my-branch # ...a branch or commit-ish
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Model id normalisation (mirrors src/claudecode.ts:normalizeClaudeModelId)
# ---------------------------------------------------------------------------

_ALREADY_DOTTED = re.compile(r"claude-.+-\d+\.\d+")
_DASH_VERSION = re.compile(r"^(claude-.+)-(\d)-(\d)(-\d{8})?$")


def normalize_claude_model_id(model: str) -> str:
    if not model:
        return model
    if _ALREADY_DOTTED.search(model):
        return model
    m = _DASH_VERSION.match(model)
    if m:
        return f"{m.group(1)}-{m.group(2)}.{m.group(3)}"
    return model


# ---------------------------------------------------------------------------
# Pricing (mirrors src/tokenEstimation.ts:calculateEstimatedCost)
# ---------------------------------------------------------------------------

# Minimal fallback used only when modelPricing.json cannot be located.
_FALLBACK_PRICING = {
    "claude-opus-4.8": {"inputCostPerMillion": 5.0, "outputCostPerMillion": 25.0,
                        "cachedInputCostPerMillion": 0.5, "cacheCreationCostPerMillion": 6.25,
                        "displayNames": ["Claude Opus 4.8"]},
    "claude-opus-4.7": {"inputCostPerMillion": 5.0, "outputCostPerMillion": 25.0,
                        "cachedInputCostPerMillion": 0.5, "cacheCreationCostPerMillion": 6.25,
                        "displayNames": ["Claude Opus 4.7"]},
    "claude-opus-4.6": {"inputCostPerMillion": 5.0, "outputCostPerMillion": 25.0,
                        "cachedInputCostPerMillion": 0.5, "cacheCreationCostPerMillion": 6.25,
                        "displayNames": ["Claude Opus 4.6"]},
    "claude-sonnet-4.6": {"inputCostPerMillion": 3.0, "outputCostPerMillion": 15.0,
                          "cachedInputCostPerMillion": 0.3, "cacheCreationCostPerMillion": 3.75,
                          "displayNames": ["Claude Sonnet 4.6"]},
    "claude-sonnet-4.5": {"inputCostPerMillion": 3.0, "outputCostPerMillion": 15.0,
                          "cachedInputCostPerMillion": 0.3, "cacheCreationCostPerMillion": 3.75,
                          "displayNames": ["Claude Sonnet 4.5"]},
    "claude-haiku-4.5": {"inputCostPerMillion": 1.0, "outputCostPerMillion": 5.0,
                         "cachedInputCostPerMillion": 0.1, "cacheCreationCostPerMillion": 1.25,
                         "displayNames": ["Claude Haiku 4.5"]},
    "gpt-4o-mini": {"inputCostPerMillion": 0.15, "outputCostPerMillion": 0.6,
                    "cachedInputCostPerMillion": 0.075, "displayNames": ["GPT-4o Mini"]},
}


def load_pricing() -> dict:
    """Load the extension's modelPricing.json if it can be found nearby, else fall back."""
    here = Path(__file__).resolve().parent
    candidates = [
        here / "modelPricing.json",
        here.parent / "vscode-extension" / "src" / "modelPricing.json",
    ]
    for path in candidates:
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                pricing = data.get("pricing", data)
                if isinstance(pricing, dict) and pricing:
                    return pricing
        except (OSError, ValueError):
            continue
    return _FALLBACK_PRICING


def calculate_estimated_cost(model_usage: dict, pricing: dict) -> float:
    """USD cost — same maths as tokenEstimation.ts:calculateEstimatedCost (provider rates)."""
    total = 0.0
    for model, usage in model_usage.items():
        entry = pricing.get(model) or pricing.get("gpt-4o-mini")
        if not entry:
            continue
        in_rate = entry.get("inputCostPerMillion", 0.0)
        out_rate = entry.get("outputCostPerMillion", 0.0)
        cached_rate = entry.get("cachedInputCostPerMillion", in_rate)
        creation_rate = entry.get("cacheCreationCostPerMillion", in_rate)
        cached_read = usage.get("cachedReadTokens", 0)
        cache_creation = usage.get("cacheCreationTokens", 0)
        uncached_input = max(0, usage["inputTokens"] - cached_read - cache_creation)
        total += (uncached_input / 1_000_000) * in_rate
        total += (cached_read / 1_000_000) * cached_rate
        total += (cache_creation / 1_000_000) * creation_rate
        total += (usage["outputTokens"] / 1_000_000) * out_rate
    return total


def model_display_name(model: str, pricing: dict) -> str:
    entry = pricing.get(model)
    if entry:
        names = entry.get("displayNames")
        if isinstance(names, list) and names:
            return names[0]
    return model


def is_mcp_tool(name: str) -> bool:
    name = str(name)
    return name.startswith("mcp.") or name.startswith("mcp_") or name.startswith("mcp__")


# ---------------------------------------------------------------------------
# Token estimation for Copilot Chat (mirrors src/tokenEstimation.ts)
# ---------------------------------------------------------------------------

import math

_DEFAULT_TOKENS_PER_CHAR = 0.25  # src/tokenEstimation.ts:DEFAULT_TOKENS_PER_CHAR


def load_token_estimators() -> dict:
    here = Path(__file__).resolve().parent
    for path in (here / "tokenEstimators.json",
                 here.parent / "vscode-extension" / "src" / "tokenEstimators.json"):
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                est = data.get("estimators", data)
                if isinstance(est, dict) and est:
                    return est
        except (OSError, ValueError):
            continue
    return {}


_TOKEN_ESTIMATORS = load_token_estimators()


def estimate_tokens(text: str, model: str = "gpt-4") -> int:
    """ceil(len(text) * tokensPerChar) with per-model overrides (tokenEstimation.ts)."""
    if not text:
        return 0
    ratio = _DEFAULT_TOKENS_PER_CHAR
    for key, val in _TOKEN_ESTIMATORS.items():
        if key in model or key.replace("-", "") in model:
            ratio = val
            break
    return math.ceil(len(text) * ratio)


# ---------------------------------------------------------------------------
# Session parsing (mirrors src/claudecode.ts + adapters/claudeCodeAdapter.ts)
# ---------------------------------------------------------------------------

class SessionSummary:
    __slots__ = ("title", "file_path", "interactions", "tool_calls", "input_tokens",
                 "output_tokens", "thinking_tokens", "cached_tokens", "total_tokens",
                 "estimated_cost", "editor", "models", "last_activity")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


def read_events(path: Path) -> list:
    events = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue  # skip malformed lines
    except OSError:
        return []
    return events


def deduplicate_assistant_events(events: list) -> list:
    """Last-wins by message.id; assistant events without an id are kept as-is.

    Mirrors claudecode.ts:deduplicateAssistantEvents — used for token/model counts only.
    """
    by_id = {}
    no_id = []
    for e in events:
        if e.get("type") != "assistant" or not e.get("message", {}).get("usage"):
            continue
        msg_id = e.get("message", {}).get("id")
        if msg_id:
            by_id[msg_id] = e
        else:
            no_id.append(e)
    return list(by_id.values()) + no_id


def count_interactions(events: list) -> int:
    """Count real user text turns (claudecode.ts:countClaudeCodeInteractions)."""
    count = 0
    for e in events:
        if e.get("type") != "user" or e.get("isSidechain"):
            continue
        msg = e.get("message") or {}
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            count += 1
        elif isinstance(content, list):
            has_text = any(c.get("type") == "text" for c in content if isinstance(c, dict))
            has_tool_result = any(c.get("type") == "tool_result" for c in content if isinstance(c, dict))
            if has_text and not has_tool_result:
                count += 1
    return count


def collect_model_usage(events: list) -> dict:
    """Per-model token usage from deduplicated assistant events (claudecode.ts:getClaudeCodeModelUsage)."""
    model_usage = {}
    for e in deduplicate_assistant_events(events):
        usage = e["message"]["usage"]
        model = normalize_claude_model_id(e.get("message", {}).get("model") or "unknown")
        mu = model_usage.setdefault(model, {"inputTokens": 0, "outputTokens": 0,
                                            "cacheCreationTokens": 0, "cachedReadTokens": 0})
        cache_creation = _num(usage.get("cache_creation_input_tokens"))
        cached_read = _num(usage.get("cache_read_input_tokens"))
        input_tokens = _num(usage.get("input_tokens")) + cache_creation + cached_read
        output_tokens = _num(usage.get("output_tokens"))
        mu["inputTokens"] += input_tokens
        mu["outputTokens"] += output_tokens
        mu["cacheCreationTokens"] += cache_creation
        mu["cachedReadTokens"] += cached_read
    return model_usage


def count_tool_calls(events: list) -> int:
    """Non-MCP tool_use blocks across raw assistant events (claudeCodeAdapter.ts:processAssistantEvent).

    Matches the extension's 'Tools' column, which is analysis.toolCalls.total and excludes MCP tools.
    """
    total = 0
    for e in events:
        if e.get("type") != "assistant":
            continue
        content = e.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            name = str(c.get("name") or "tool")
            if not is_mcp_tool(name):
                total += 1
    return total


def extract_meta(events: list) -> dict:
    title = None
    entrypoint = None
    cwd = None
    timestamps = []
    for e in events:
        if e.get("type") == "ai-title" and e.get("aiTitle"):
            title = e["aiTitle"]
        if not entrypoint and e.get("entrypoint"):
            entrypoint = e["entrypoint"]
        if not cwd and e.get("cwd"):
            cwd = e["cwd"]
        ts = e.get("timestamp")
        if ts:
            parsed = _parse_ts(ts)
            if parsed is not None:
                timestamps.append(parsed)
    first = last = None
    if timestamps:
        timestamps.sort()
        first = datetime.fromtimestamp(timestamps[0] / 1000, tz=timezone.utc)
        last = datetime.fromtimestamp(timestamps[-1] / 1000, tz=timezone.utc)
    return {"title": title, "entrypoint": entrypoint, "cwd": cwd,
            "first_interaction": first, "last_interaction": last}


def first_user_prompt(events: list) -> str:
    """Friendly fallback title: snippet of the first real user message."""
    for e in events:
        if e.get("type") != "user" or e.get("isSidechain"):
            continue
        msg = e.get("message") or {}
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        text = None
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                    text = c["text"]
                    break
        if text:
            text = re.sub(r"<ide_selection>.*?</ide_selection>", "", text, flags=re.DOTALL)
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                return text[:60] + ("…" if len(text) > 60 else "")
    return ""


def summarize_session(path: Path, pricing: dict) -> SessionSummary | None:
    events = read_events(path)
    if not events:
        return None
    interactions = count_interactions(events)
    if interactions == 0:
        return None  # extension skips zero-interaction sessions

    model_usage = collect_model_usage(events)
    meta = extract_meta(events)

    input_tok = sum(mu["inputTokens"] for mu in model_usage.values())
    output_tok = sum(mu["outputTokens"] for mu in model_usage.values())
    cached_tok = sum(mu["cachedReadTokens"] for mu in model_usage.values())
    total_tok = input_tok + output_tok  # Claude has no separate actualTokens

    last_dt = meta["last_interaction"] or _mtime_dt(path)
    title = meta["title"] or first_user_prompt(events) or "Untitled session"

    return SessionSummary(
        title=title,
        file_path=str(path),
        interactions=interactions,
        tool_calls=count_tool_calls(events),
        input_tokens=input_tok,
        output_tokens=output_tok,
        thinking_tokens=0,  # Claude folds thinking into output_tokens
        cached_tokens=cached_tok,
        total_tokens=total_tok,
        estimated_cost=calculate_estimated_cost(model_usage, pricing),
        editor=_editor_label(meta.get("entrypoint")),
        models=[model_display_name(m, pricing) for m in model_usage.keys()],
        last_activity=last_dt,
    )


# ---------------------------------------------------------------------------
# GitHub Copilot Chat parsing (mirrors src/sessionParser.ts + tokenEstimation.ts +
# usageAnalysis.ts delta path). Delta-based JSONL (kind:0/1/2) is reconstructed into
# a session state, then per-request metrics are computed.
# ---------------------------------------------------------------------------

def apply_delta(state, delta):
    """Reconstruct session state from a delta event (sessionParser.ts:applyDelta).

    kind 0 = full replace, 1 = set at key path, 2 = append to array at key path.
    """
    if not isinstance(delta, dict):
        return state
    kind, k, v = delta.get("kind"), delta.get("k"), delta.get("v")
    if kind == 0:
        return v
    if not isinstance(k, list) or not k:
        return state
    path = [str(x) for x in k]
    root = state if isinstance(state, (dict, list)) else {}
    cur = root
    for i in range(len(path) - 1):
        seg, nxt = path[i], path[i + 1]
        if isinstance(cur, list) and seg.isdigit():
            idx = int(seg)
            while len(cur) <= idx:
                cur.append(None)
            if not isinstance(cur[idx], (dict, list)):
                cur[idx] = [] if nxt.isdigit() else {}
            cur = cur[idx]
        elif isinstance(cur, dict):
            if not isinstance(cur.get(seg), (dict, list)):
                cur[seg] = [] if nxt.isdigit() else {}
            cur = cur[seg]
        else:
            return root
    last = path[-1]
    if kind == 1:
        if isinstance(cur, list) and last.isdigit():
            idx = int(last)
            while len(cur) <= idx:
                cur.append(None)
            cur[idx] = v
        elif isinstance(cur, dict):
            cur[last] = v
    elif kind == 2:
        target = None
        if isinstance(cur, list) and last.isdigit():
            idx = int(last)
            while len(cur) <= idx:
                cur.append(None)
            if not isinstance(cur[idx], list):
                cur[idx] = []
            target = cur[idx]
        elif isinstance(cur, dict):
            if not isinstance(cur.get(last), list):
                cur[last] = []
            target = cur[last]
        if target is not None:
            target.extend(v if isinstance(v, list) else [v])
    return root


def reconstruct_copilot_state(path: Path):
    """Return (state, is_delta) for a Copilot Chat session file, or (None, False)."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None, False
    stripped = content.strip()
    if not stripped:
        return None, False
    lines = [ln for ln in stripped.split("\n") if ln.strip()]
    # Delta-based JSONL: first parseable line carries a numeric "kind".
    first = _safe_json(lines[0])
    if isinstance(first, dict) and isinstance(first.get("kind"), (int, float)):
        state = {}
        for ln in lines:
            d = _safe_json(ln)
            if d is not None:
                state = apply_delta(state, d)
        return (state if isinstance(state, dict) else None), True
    # Plain JSON object with a requests/history array.
    obj = _safe_json(stripped)
    if isinstance(obj, dict):
        return obj, False
    return None, False


def _resp_item_text(item):
    """(text, is_thinking) for a response item (tokenEstimation.ts:extractResponseItemText)."""
    if not isinstance(item, dict):
        return "", False
    if item.get("kind") == "thinking":
        v = item.get("value")
        return (v if isinstance(v, str) else ""), True
    content = item.get("content")
    if isinstance(content, dict) and isinstance(content.get("value"), str) and content["value"]:
        return content["value"], False
    v = item.get("value")
    if isinstance(v, str) and v:
        return v, False
    return "", False


def _request_model(req) -> str:
    """Model id with the copilot/ prefix stripped (usageAnalysis.ts:_pdsaGetReqModel)."""
    raw = (req.get("modelId")
           or (req.get("selectedModel") or {}).get("identifier")
           or ((req.get("result") or {}).get("metadata") or {}).get("modelId")
           or req.get("model"))
    if isinstance(raw, str) and raw.strip():
        return re.sub(r"^copilot/", "", raw.strip())
    return "unknown"


def _request_actual_tokens(req):
    """(promptTokens, outputTokens) from a request result (tokenEstimation.ts:_dtsExtractFromResult)."""
    res = req.get("result")
    if not isinstance(res, dict):
        return 0, 0
    md = res.get("metadata")
    if isinstance(md, dict) and isinstance(md.get("promptTokens"), (int, float)) \
            and isinstance(md.get("outputTokens"), (int, float)):
        return int(md["promptTokens"]), int(md["outputTokens"])
    if isinstance(res.get("promptTokens"), (int, float)) and isinstance(res.get("outputTokens"), (int, float)):
        return int(res["promptTokens"]), int(res["outputTokens"])
    usage = res.get("usage")
    if isinstance(usage, dict):
        return int(_num(usage.get("promptTokens"))), int(_num(usage.get("completionTokens")))
    return 0, 0


def _request_timestamp(req):
    ts = req.get("timestamp")
    return _parse_ts(ts)


def _copilot_editor_label(path: Path) -> str:
    mapping = {
        "Code": "VS Code",
        "Code - Insiders": "VS Code - Insiders",
        "Code - Exploration": "VS Code - Exploration",
        "VSCodium": "VSCodium",
        "Cursor": "Cursor",
    }
    parts = path.parts
    for variant, label in mapping.items():
        if variant in parts:
            return label
    return "VS Code"


def summarize_copilot_session(path: Path, pricing: dict):
    state, is_delta = reconstruct_copilot_state(path)
    if not isinstance(state, dict):
        return None
    requests = state.get("requests")
    if not isinstance(requests, list):
        requests = state.get("history") if isinstance(state.get("history"), list) else []
    if not requests:
        return None

    interactions = 0
    tool_calls = 0
    est_input = est_output = thinking = 0
    actual_input = actual_output = 0
    actual_by_model = {}   # model -> {inputTokens, outputTokens}
    est_by_model = {}      # model -> {inputTokens, outputTokens}
    timestamps = []

    for req in requests:
        if not isinstance(req, dict):
            continue
        model = _request_model(req)
        msg = req.get("message") or {}
        text = msg.get("text") if isinstance(msg.get("text"), str) else None
        if text and text.strip():
            interactions += 1

        # Estimated input from the request message.
        ein = 0
        if isinstance(msg.get("parts"), list):
            for p in msg["parts"]:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    ein += estimate_tokens(p["text"], model)
        elif isinstance(text, str):
            ein += estimate_tokens(text, model)

        # Response items: estimated output / thinking + tool invocations.
        eout = 0
        responses = req.get("response")
        if not isinstance(responses, list):
            responses = req.get("responses") if isinstance(req.get("responses"), list) else []
        for it in responses:
            t, is_think = _resp_item_text(it)
            if t:
                if is_think:
                    thinking += estimate_tokens(t)
                else:
                    eout += estimate_tokens(t)
            if isinstance(it, dict) and it.get("kind") in ("toolInvocationSerialized", "prepareToolInvocation"):
                tsd = it.get("toolSpecificData") or {}
                name = (it.get("toolId") or it.get("toolName")
                        or (it.get("invocationMessage") or {}).get("toolName")
                        or tsd.get("kind") or "unknown")
                if not is_mcp_tool(name):
                    tool_calls += 1
        # Each request carrying an agent counts as a tool call (usageAnalysis.ts:_pdsaProcessRequest).
        if req.get("requestId") and isinstance(req.get("agent"), dict) and req["agent"].get("id"):
            tool_calls += 1

        est_input += ein
        est_output += eout
        em = est_by_model.setdefault(model, {"inputTokens": 0, "outputTokens": 0})
        em["inputTokens"] += ein
        em["outputTokens"] += eout

        ain, aout = _request_actual_tokens(req)
        actual_input += ain
        actual_output += aout
        if ain or aout:
            am = actual_by_model.setdefault(model, {"inputTokens": 0, "outputTokens": 0})
            am["inputTokens"] += ain
            am["outputTokens"] += aout

        ts = _request_timestamp(req)
        if ts is not None:
            timestamps.append(ts)

    if interactions == 0:
        return None  # extension skips zero-interaction sessions

    has_actual = (actual_input + actual_output) > 0
    if has_actual:
        input_tok, output_tok = actual_input, actual_output
        model_usage = actual_by_model
        total_tok = actual_input + actual_output
    else:
        input_tok, output_tok = est_input, est_output
        model_usage = est_by_model
        total_tok = est_input + est_output + thinking

    # Last activity: newest request timestamp → lastMessageDate → creationDate → mtime.
    if timestamps:
        last_dt = datetime.fromtimestamp(max(timestamps) / 1000, tz=timezone.utc)
    else:
        epoch = _parse_ts(state.get("lastMessageDate")) or _parse_ts(state.get("creationDate"))
        last_dt = datetime.fromtimestamp(epoch / 1000, tz=timezone.utc) if epoch else _mtime_dt(path)

    title = (state.get("customTitle") or state.get("title")
             or _copilot_first_prompt(requests) or "Untitled session")

    return SessionSummary(
        title=title,
        file_path=str(path),
        interactions=interactions,
        tool_calls=tool_calls,
        input_tokens=input_tok,
        output_tokens=output_tok,
        thinking_tokens=thinking,
        cached_tokens=0,  # delta sessions expose no cache-read counts
        total_tokens=total_tok,
        estimated_cost=calculate_estimated_cost(model_usage, pricing),
        editor=_copilot_editor_label(path),
        models=[model_display_name(m, pricing) for m in model_usage.keys()] or ["unknown"],
        last_activity=last_dt,
    )


def _copilot_first_prompt(requests: list) -> str:
    for req in requests:
        if not isinstance(req, dict):
            continue
        msg = req.get("message") or {}
        text = msg.get("text") if isinstance(msg.get("text"), str) else None
        if text and text.strip():
            t = re.sub(r"\s+", " ", text).strip()
            return t[:60] + ("…" if len(t) > 60 else "")
    return ""


def _safe_json(line: str):
    try:
        return json.loads(line)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def claude_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def discover_claude_files() -> list:
    base = claude_projects_dir()
    if not base.is_dir():
        return []
    files = []
    for project in base.iterdir():
        if not project.is_dir():
            continue
        for f in project.glob("*.jsonl"):
            try:
                if f.is_file() and f.stat().st_size > 0:
                    files.append(f)
            except OSError:
                continue
    return files


_VSCODE_VARIANTS = ["Code", "Code - Insiders", "Code - Exploration", "VSCodium", "Cursor"]
_COPILOT_EXT_FOLDERS = ["GitHub.copilot-chat", "github.copilot-chat", "GitHub.copilot", "github.copilot"]
_COPILOT_SUBDIRS = ["chatSessions", "debug-logs", "transcripts"]
_COPILOT_SKIP_NAMES = {"sessions.json", "state.json"}


def _vscode_user_dirs() -> list:
    """Candidate VS Code 'User' directories (copilotChatAdapter.ts:getVSCodeUserPaths)."""
    home = Path.home()
    dirs = []
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        for v in _VSCODE_VARIANTS:
            dirs.append(appdata / v / "User")
    elif sys.platform == "darwin":
        for v in _VSCODE_VARIANTS:
            dirs.append(home / "Library" / "Application Support" / v / "User")
    else:
        xdg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        for v in _VSCODE_VARIANTS:
            dirs.append(xdg / v / "User")
    for remote in (".vscode-server", ".vscode-server-insiders", ".vscode-remote"):
        dirs.append(home / remote / "data" / "User")
    return dirs


def discover_copilot_files() -> list:
    files = []
    for user_dir in _vscode_user_dirs():
        ws_storage = user_dir / "workspaceStorage"
        if not ws_storage.is_dir():
            continue
        for ws in ws_storage.iterdir():
            if not ws.is_dir():
                continue
            leaf_dirs = [ws / "chatSessions"]
            for ext in _COPILOT_EXT_FOLDERS:
                for sub in _COPILOT_SUBDIRS:
                    leaf_dirs.append(ws / ext / sub)
            for leaf in leaf_dirs:
                if not leaf.is_dir():
                    continue
                for f in leaf.iterdir():
                    if f.suffix not in (".json", ".jsonl"):
                        continue
                    if f.name in _COPILOT_SKIP_NAMES:
                        continue
                    try:
                        if f.is_file() and f.stat().st_size > 0:
                            files.append(f)
                    except OSError:
                        continue
        # globalStorage/emptyWindowChatSessions (windowless chats)
        empty_win = user_dir / "globalStorage" / "emptyWindowChatSessions"
        if empty_win.is_dir():
            for f in empty_win.iterdir():
                if f.suffix in (".json", ".jsonl") and f.name not in _COPILOT_SKIP_NAMES:
                    try:
                        if f.is_file() and f.stat().st_size > 0:
                            files.append(f)
                    except OSError:
                        continue
    return files


def collect_summaries(pricing: dict) -> list:
    summaries = []
    for f in discover_claude_files():
        s = summarize_session(f, pricing)
        if s:
            summaries.append(s)
    for f in discover_copilot_files():
        try:
            s = summarize_copilot_session(f, pricing)
        except Exception:  # noqa: BLE001 — never let one malformed session abort the run
            s = None
        if s:
            summaries.append(s)
    return summaries


# ---------------------------------------------------------------------------
# PR ↔ session matching ("which session produced this PR?")
#
# A session is scored against a PR/commit/branch by how strongly its evidence
# overlaps the change set:
#   * files the session EDITED that the PR also changed   (strongest)
#   * files the session READ   that the PR also changed   (supporting)
#   * the session's git branch equals the PR branch       (boost)
#   * the session was active near the PR's commit time     (tie-breaker)
# Only sessions whose working directory is inside the repo are considered.
# ---------------------------------------------------------------------------

_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit",
               "create_file", "edit_file", "str_replace_editor", "apply_patch"}
_READ_TOOLS = {"Read", "read_file", "view"}


def _git(args: list, cwd: Path):
    """Run a git command, returning stripped stdout or None on failure."""
    try:
        out = subprocess.run(["git", "-C", str(cwd), *args],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def _gh_json(args: list, cwd: Path):
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True,
                             timeout=30, cwd=str(cwd))
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def _norm_path(p: str) -> str:
    """Absolute, OS-normalised, case-folded path for cross-comparison."""
    try:
        return os.path.normcase(os.path.normpath(os.path.abspath(p)))
    except (TypeError, ValueError):
        return ""


def resolve_pr_target(ref, repo_root: Path) -> dict | None:
    """Resolve a PR/commit/branch reference into a change set.

    `ref` may be a PR number (resolved via `gh` when available), a branch name,
    a commit-ish, or None/"" to mean "the current branch's diff against main".
    Returns {files:set[str], branch:str|None, when:datetime|None, source:str}.
    File paths are absolute + normalised for matching.
    """
    rel_files: list[str] = []
    branch = None
    when = None
    source = ""

    ref_str = (ref or "").strip()

    # 1. PR number → GitHub via gh.
    if ref_str.isdigit():
        data = _gh_json(["pr", "view", ref_str, "--json",
                         "headRefName,files,commits,number"], repo_root)
        if data:
            branch = data.get("headRefName")
            rel_files = [f.get("path") for f in (data.get("files") or []) if f.get("path")]
            commits = data.get("commits") or []
            if commits:
                when = _gh_dt(commits[-1].get("committedDate") or commits[-1].get("authoredDate"))
            source = f"PR #{ref_str} ({branch or 'unknown branch'}) via gh"
        if not rel_files:
            print(f"Could not resolve PR #{ref_str} via gh (gh not installed, not "
                  f"authenticated, or PR not found). Falling back to the current branch.",
                  file=sys.stderr)
            # Try a local branch/commit of the same name, else current branch.
            ref_str = ref_str if _git(["rev-parse", "--verify", ref_str], repo_root) else ""

    # 2. Branch / commit-ish, or current branch when ref is empty.
    if not rel_files:
        if ref_str:
            is_branch = bool(_git(["show-ref", "--verify", "--quiet", f"refs/heads/{ref_str}"], repo_root) is not None
                             and _git(["rev-parse", "--verify", f"refs/heads/{ref_str}"], repo_root))
            branch = branch or (ref_str if is_branch else None)
            base = _git(["merge-base", "origin/main", ref_str], repo_root) \
                or _git(["merge-base", "main", ref_str], repo_root)
            diff = _git(["diff", "--name-only", f"{base}...{ref_str}"], repo_root) if base else None
            if not diff:  # treat ref as a single commit
                diff = _git(["diff-tree", "--no-commit-id", "--name-only", "-r", ref_str], repo_root)
                when = _gh_dt(_git(["show", "-s", "--format=%cI", ref_str], repo_root))
                source = source or f"commit {ref_str[:12]}"
            else:
                when = _gh_dt(_git(["show", "-s", "--format=%cI", ref_str], repo_root))
                source = source or f"branch {ref_str} vs main"
            rel_files = [l for l in (diff or "").splitlines() if l]
        else:
            branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root)
            base = _git(["merge-base", "origin/main", "HEAD"], repo_root) \
                or _git(["merge-base", "main", "HEAD"], repo_root)
            diff = _git(["diff", "--name-only", f"{base}...HEAD"], repo_root) if base else None
            if not diff:  # on main with nothing ahead → fall back to the last commit
                diff = _git(["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"], repo_root)
                source = f"last commit on {branch or 'HEAD'}"
            else:
                source = f"current branch {branch} vs main"
            when = _gh_dt(_git(["show", "-s", "--format=%cI", "HEAD"], repo_root))
            rel_files = [l for l in (diff or "").splitlines() if l]

    if not rel_files:
        return None

    abs_files = {_norm_path(str(repo_root / f)) for f in rel_files}
    abs_files.discard("")
    return {"files": abs_files, "rel_files": rel_files, "branch": branch,
            "when": when, "source": source}


def _gh_dt(value):
    if not value:
        return None
    epoch = _parse_ts(value)
    return datetime.fromtimestamp(epoch / 1000, tz=timezone.utc) if epoch else None


def extract_file_signals(events: list) -> dict:
    """Collect a session's working dirs, branches, and edited/read file paths."""
    cwds, branches, edited, read = set(), set(), set(), set()
    for e in events:
        if e.get("cwd"):
            cwds.add(_norm_path(e["cwd"]))
        if e.get("gitBranch"):
            branches.add(e["gitBranch"])
        if e.get("type") != "assistant":
            continue
        content = e.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            inp = b.get("input") or {}
            fp = inp.get("file_path") or inp.get("notebook_path") or inp.get("path")
            if not fp:
                continue
            name = b.get("name") or ""
            target = edited if name in _EDIT_TOOLS else (read if name in _READ_TOOLS else None)
            if target is not None:
                target.add(_norm_path(fp))
    cwds.discard(""); edited.discard(""); read.discard("")
    return {"cwds": cwds, "branches": branches, "edited": edited, "read": read}


def _uri_to_path(obj) -> str:
    """Normalise a VS Code file URI (dict or string) to an absolute OS path."""
    raw = ""
    if isinstance(obj, dict):
        raw = obj.get("fsPath") or obj.get("path") or obj.get("external") or ""
    elif isinstance(obj, str):
        raw = obj
    if not raw:
        return ""
    if raw.startswith("file://"):
        from urllib.parse import unquote, urlparse
        p = urlparse(raw)
        raw = unquote(p.path)
        if p.netloc:  # UNC: file://server/share → //server/share
            raw = "//" + p.netloc + raw
    else:
        from urllib.parse import unquote
        if "%" in raw:
            raw = unquote(raw)
    # "/c:/Users/.." → "c:/Users/.." (URI path form for Windows drives).
    m = re.match(r"^/([A-Za-z]:[\\/].*)$", raw)
    if m:
        raw = m.group(1)
    return _norm_path(raw)


def _file_uris(obj, out: set) -> None:
    """Recursively collect every file:// URI (as normalised path) in a value."""
    if isinstance(obj, dict):
        if obj.get("scheme") == "file" and (obj.get("fsPath") or obj.get("path")):
            p = _uri_to_path(obj)
            if p:
                out.add(p)
        for v in obj.values():
            _file_uris(v, out)
    elif isinstance(obj, list):
        for x in obj:
            _file_uris(x, out)


def _copilot_workspace_root(path: Path):
    """Resolve a Copilot session file to its workspace folder via workspace.json."""
    hash_dir = None
    for parent in path.parents:
        if parent.parent is not None and parent.parent.name == "workspaceStorage":
            hash_dir = parent
            break
    if hash_dir is None:
        return None  # e.g. globalStorage/emptyWindowChatSessions — no bound folder
    wj = hash_dir / "workspace.json"
    try:
        data = json.loads(wj.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    folder = data.get("folder")
    if not isinstance(folder, str):
        return None
    return _uri_to_path(folder)


def extract_copilot_file_signals(state: dict) -> dict:
    """Collect edited vs. referenced file paths from a reconstructed Copilot session."""
    edited, referenced = set(), set()
    requests = state.get("requests")
    if not isinstance(requests, list):
        requests = state.get("history") if isinstance(state.get("history"), list) else []
    for req in requests:
        if not isinstance(req, dict):
            continue
        # Applied edits are the strong signal: textEditGroup items carry the target uri.
        responses = req.get("response") if isinstance(req.get("response"), list) else []
        for it in responses:
            if isinstance(it, dict) and it.get("kind") == "textEditGroup":
                _file_uris(it.get("uri"), edited)
        # Everything else the turn touched (context, references, tool reads) = referenced.
        for key in ("message", "variableData", "contentReferences", "response"):
            _file_uris(req.get(key), referenced)
    edited.discard("")
    referenced -= edited
    referenced.discard("")
    return {"edited": edited, "read": referenced}


def _session_in_repo(signals: dict, repo_root_norm: str) -> bool:
    for c in signals["cwds"]:
        if c == repo_root_norm or c.startswith(repo_root_norm + os.sep):
            return True
    return False


def match_pr(args, pricing: dict) -> int:
    repo_root = Path(os.getcwd()).resolve()
    if not _git(["rev-parse", "--is-inside-work-tree"], repo_root):
        print(f"Not inside a git repository: {repo_root}", file=sys.stderr)
        return 1

    target = resolve_pr_target(args.pr, repo_root)
    if not target:
        print("Could not determine any changed files for that reference. "
              "Pass a PR number, branch, or commit — or run from a branch with commits ahead of main.",
              file=sys.stderr)
        return 1

    repo_root_norm = _norm_path(str(repo_root))
    pr_files = target["files"]
    pr_branch = target["branch"]
    pr_when = target["when"]
    # Map case-folded match key → original-case repo-relative path for display.
    pr_disp = {_norm_path(str(repo_root / f)): f.replace("\\", "/") for f in target["rel_files"]}

    candidates = []

    def add_candidate(summary, edited, read, branch_match):
        """Score a session against the change set and record it if it matches."""
        edited_hits = sorted(edited & pr_files)
        read_hits = sorted((read & pr_files) - set(edited_hits))
        if not edited_hits and not read_hits and not branch_match:
            return
        if summary is None:  # zero-interaction session — skipped by the extension too
            return
        proximity = 0.0
        if pr_when is not None:
            hours = abs((summary.last_activity - pr_when).total_seconds()) / 3600.0
            if hours <= 48:
                proximity = 3.0 * (1 - hours / 48)  # 0..3, fades over two days
        score = len(edited_hits) * 5 + len(read_hits) * 1 \
            + (6 if branch_match else 0) + proximity
        candidates.append({
            "score": score,
            "branch_match": branch_match,
            "edited_hits": [pr_disp.get(h, _rel(h, repo_root)) for h in edited_hits],
            "read_hits": [pr_disp.get(h, _rel(h, repo_root)) for h in read_hits],
            "summary": summary,
        })

    # Claude Code sessions — repo via per-event cwd, branch via gitBranch.
    for path in discover_claude_files():
        events = read_events(path)
        if not events:
            continue
        signals = extract_file_signals(events)
        if not _session_in_repo(signals, repo_root_norm):
            continue
        branch_match = bool(pr_branch and pr_branch in signals["branches"])
        add_candidate(summarize_session(path, pricing),
                      signals["edited"], signals["read"], branch_match)

    # Copilot Chat (and other VS Code) sessions — repo via workspace.json, no git branch.
    for path in discover_copilot_files():
        root = _copilot_workspace_root(path)
        if root is None or _norm_path(root) != repo_root_norm:
            continue
        state, _ = reconstruct_copilot_state(path)
        if not isinstance(state, dict):
            continue
        signals = extract_copilot_file_signals(state)
        try:
            summary = summarize_copilot_session(path, pricing)
        except Exception:  # noqa: BLE001 — never let one malformed session abort the run
            summary = None
        add_candidate(summary, signals["edited"], signals["read"], False)

    # Best match first; ties broken by recency.
    candidates.sort(key=lambda c: (c["score"], c["summary"].last_activity), reverse=True)
    top = candidates[: max(1, args.top)]
    summaries = [c["summary"] for c in top]

    if args.json:
        out = []
        for c in top:
            d = c["summary"].as_dict()
            d["last_activity"] = c["summary"].last_activity.astimezone().isoformat()
            d["match_score"] = round(c["score"], 2)
            d["branch_match"] = c["branch_match"]
            d["matched_edited_files"] = c["edited_hits"]
            d["matched_read_files"] = c["read_hits"]
            out.append(d)
        print(json.dumps({
            "source": target["source"],
            "branch": pr_branch,
            "changed_files": target["rel_files"],
            "matches": out,
        }, indent=2))
        return 0

    print(f"\n🔎 Sessions for: {target['source']}")
    print(f"   Changed files ({len(pr_files)}): "
          + (", ".join(target["rel_files"][:6]) + (" …" if len(target["rel_files"]) > 6 else "")))
    if not summaries:
        print("\nNo local session (Claude Code or Copilot) in this repo touched those files.\n")
        return 0
    print("   Ranked by match confidence (best match first).\n")

    print(render_table(summaries))
    print()
    print(render_totals(summaries))

    # Brief why-it-matched footnote so the ranking is explainable.
    print()
    for rank, c in enumerate(top, 1):
        bits = []
        if c["edited_hits"]:
            bits.append(f"edited {len(c['edited_hits'])} PR file(s)")
        if c["read_hits"]:
            bits.append(f"read {len(c['read_hits'])}")
        if c["branch_match"]:
            bits.append("branch match ⭐")
        title = _truncate(c["summary"].title, 40)
        print(f"  {rank}. {title} — {', '.join(bits) or 'weak match'} "
              f"(score {c['score']:.1f})")
    print()
    return 0


def _rel(norm_path: str, repo_root: Path) -> str:
    try:
        return os.path.relpath(norm_path, _norm_path(str(repo_root))).replace("\\", "/")
    except ValueError:
        return norm_path


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _num(v) -> int:
    return v if isinstance(v, (int, float)) else 0


def _parse_ts(ts):
    if isinstance(ts, (int, float)):
        return int(ts)
    if isinstance(ts, str):
        try:
            s = ts.replace("Z", "+00:00")
            return int(datetime.fromisoformat(s).timestamp() * 1000)
        except ValueError:
            return None
    return None


def _mtime_dt(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _editor_label(entrypoint) -> str:
    # The extension's detectEditorSource labels all ~/.claude sessions "Claude Code".
    return "Claude Code"


def local_day_key(dt: datetime) -> str:
    """Local-timezone YYYY-MM-DD key (extension uses toLocalDayKey on lastActivity)."""
    return dt.astimezone().strftime("%Y-%m-%d")


def fmt_int(n) -> str:
    return f"{int(n):,}"


def fmt_cost(c) -> str:
    return f"${c:.4f}" if c and c > 0 else "—"


def fmt_time(dt: datetime) -> str:
    return dt.astimezone().strftime("%H:%M")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

SORT_KEYS = {
    "title": lambda s: (s.title or "").lower(),
    "interactions": lambda s: s.interactions,
    "toolCalls": lambda s: s.tool_calls,
    "inputTokens": lambda s: s.input_tokens,
    "outputTokens": lambda s: s.output_tokens,
    "thinkingTokens": lambda s: s.thinking_tokens,
    "cachedTokens": lambda s: s.cached_tokens,
    "totalTokens": lambda s: s.total_tokens,
    "estimatedCost": lambda s: s.estimated_cost,
    "editor": lambda s: (s.editor or "").lower(),
    "lastActivity": lambda s: s.last_activity,
}

# (header, attribute accessor, alignment) — mirrors the extension's column order.
COLUMNS = [
    ("#", None, ">"),
    ("Title", lambda s: s.title, "<"),
    ("Turns", lambda s: fmt_int(s.interactions), ">"),
    ("Tools", lambda s: fmt_int(s.tool_calls), ">"),
    ("Input", lambda s: fmt_int(s.input_tokens), ">"),
    ("Output", lambda s: fmt_int(s.output_tokens), ">"),
    ("Thinking", lambda s: fmt_int(s.thinking_tokens), ">"),
    ("Cached", lambda s: fmt_int(s.cached_tokens), ">"),
    ("Total", lambda s: fmt_int(s.total_tokens), ">"),
    ("Cost", lambda s: fmt_cost(s.estimated_cost), ">"),
    ("Editor", lambda s: s.editor, "<"),
    ("Models", lambda s: ", ".join(s.models) or "—", "<"),
    ("Last Active", lambda s: fmt_time(s.last_activity), ">"),
]

TITLE_MAX = 40
MODELS_MAX = 26


def _truncate(text: str, width: int) -> str:
    text = text or ""
    return text if len(text) <= width else text[: width - 1] + "…"


def render_table(sessions: list) -> str:
    rows = []
    for idx, s in enumerate(sessions, start=1):
        cells = []
        for header, accessor, _align in COLUMNS:
            if accessor is None:
                cells.append(str(idx))
            else:
                val = accessor(s)
                if header == "Title":
                    val = _truncate(val, TITLE_MAX)
                elif header == "Models":
                    val = _truncate(val, MODELS_MAX)
                cells.append(val)
        rows.append(cells)

    headers = [c[0] for c in COLUMNS]
    aligns = [c[2] for c in COLUMNS]
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells):
        return "  ".join(f"{cell:{aligns[i]}{widths[i]}}" for i, cell in enumerate(cells))

    sep = "  ".join("-" * w for w in widths)
    lines = [fmt_row(headers), sep]
    lines.extend(fmt_row(row) for row in rows)
    return "\n".join(lines)


def render_totals(sessions: list) -> str:
    return (
        f"Sessions: {len(sessions)}   "
        f"Turns: {fmt_int(sum(s.interactions for s in sessions))}   "
        f"Tools: {fmt_int(sum(s.tool_calls for s in sessions))}   "
        f"Total tokens: {fmt_int(sum(s.total_tokens for s in sessions))}   "
        f"Cost: {fmt_cost(sum(s.estimated_cost for s in sessions))}"
    )


def delete_all_sessions(args) -> int:
    """Discover local session files and delete them (destructive).

    Prompts for confirmation unless `args.yes` is true.
    """
    claude = discover_claude_files()
    copilot = discover_copilot_files()
    files = sorted({str(p) for p in (claude + copilot)})
    if not files:
        print("No Claude Code or Copilot Chat session files found to delete.")
        return 0

    print(f"Found {len(files)} session file(s):")
    for f in files:
        print("  ", f)

    if not getattr(args, "yes", False):
        try:
            ans = input("Delete these files? Type 'yes' to confirm: ")
        except EOFError:
            ans = ""
        if ans.strip().lower() not in ("yes", "y"):
            print("Aborted.")
            return 1

    removed = 0
    failed = []
    for fp in files:
        try:
            Path(fp).unlink()
            removed += 1
        except Exception:
            try:
                os.remove(fp)
                removed += 1
            except Exception as exc:
                failed.append((fp, str(exc)))

    print(f"Deleted {removed} file(s).")
    if failed:
        print(f"Failed to delete {len(failed)} file(s):", file=sys.stderr)
        for p, err in failed:
            print(f"  {p}: {err}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(description="Show 'Today's Sessions' metrics like the AI Engineering Fluency extension.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--date", help="Show sessions for a specific local day (YYYY-MM-DD).")
    g.add_argument("--all", action="store_true", help="Show every session regardless of date.")
    g.add_argument("--days", type=int, help="Show sessions active in the last N days.")
    g.add_argument("--pr", nargs="?", const="", metavar="REF",
                   help="Identify which session(s) produced a PR/commit/branch. "
                        "REF may be a PR number (via gh), a branch, or a commit; "
                        "omit REF to use the current branch's diff against main.")
    g.add_argument("--deleteAll", action="store_true",
                   help="Delete all found local Claude/Copilot session files (destructive).")
    p.add_argument("--top", type=int, default=10, help="Max ranked matches for --pr (default 10).")
    p.add_argument("--sort", choices=sorted(SORT_KEYS), default="interactions",
                   help="Sort column (default: interactions).")
    p.add_argument("--asc", action="store_true", help="Sort ascending (default descending).")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Assume yes to any confirmation prompts (use with --deleteAll).")
    return p.parse_args(argv)


def _in_window(s: SessionSummary, args, today_key: str) -> bool:
    if args.all:
        return True
    if args.days is not None:
        age = (datetime.now(timezone.utc) - s.last_activity).total_seconds()
        return age <= args.days * 86400
    target = args.date or today_key
    return local_day_key(s.last_activity) == target


def _force_utf8_stdout():
    """Windows consoles default to cp1252 and choke on emoji/box chars; force UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        reconfig = getattr(stream, "reconfigure", None)
        if reconfig:
            try:
                reconfig(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv=None):
    _force_utf8_stdout()
    args = parse_args(sys.argv[1:] if argv is None else argv)
    pricing = load_pricing()
    if getattr(args, "deleteAll", False):
        return delete_all_sessions(args)

    if args.pr is not None:
        return match_pr(args, pricing)

    all_summaries = collect_summaries(pricing)
    if not all_summaries:
        print("No Claude Code or Copilot Chat sessions found on this machine.", file=sys.stderr)
        return 1

    today_key = local_day_key(datetime.now(timezone.utc))
    summaries = [s for s in all_summaries if _in_window(s, args, today_key)]

    reverse = not args.asc
    summaries.sort(key=SORT_KEYS[args.sort], reverse=reverse)

    if args.json:
        out = []
        for s in summaries:
            d = s.as_dict()
            d["last_activity"] = s.last_activity.astimezone().isoformat()
            out.append(d)
        print(json.dumps(out, indent=2))
        return 0

    scope = (
        "all time" if args.all
        else f"last {args.days} day(s)" if args.days is not None
        else f"{args.date}" if args.date
        else f"today ({today_key})"
    )
    print(f"\n📊 Sessions — {scope}")
    print("Individual session breakdown, sorted by "
          f"{args.sort} ({'ascending' if args.asc else 'descending'}).\n")

    if not summaries:
        print("No sessions recorded for this period yet.\n")
        return 0

    print(render_table(summaries))
    print()
    print(render_totals(summaries))
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
