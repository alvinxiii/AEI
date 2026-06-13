# Session Insights (Python)

A standalone, dependency-free Python replica of the **Today's Sessions** table from the
AI Engineering Fluency VS Code extension
([`vscode-extension/src/webview/usage/main.ts`](../vscode-extension/src/webview/usage/main.ts)),
plus a `--pr` mode that pinpoints **which session produced a given PR/commit/branch**.

It prints an **individual session breakdown for today**, sorted by number of
interactions (most active first) — the same view, in your terminal.

```
📊 Sessions — today (2026-06-11)
Individual session breakdown, sorted by interactions (descending).

#  Title                             Turns  Tools       Input   Output  Thinking      Cached       Total       Cost  Editor       Models           Last Active
1  Learn about GitHub custom agents     42    111  42,609,020  215,921         0  41,535,137  42,824,941  $218.4431  Claude Code  Claude Opus 4.8  00:17
2  Fix TikTok video extraction err…      7     61   2,746,189   38,204         0   2,617,736   2,784,393   $14.6860  Claude Code  Claude Opus 4.8  20:37
3  TikTok extraction error report        5     32     285,600    3,072    14,545           0     288,672    $0.0447  VS Code      auto             20:11
...
```

## Data sources

It reads the **same two local sources the extension reads**:

| Source | Location | Tokens |
|--------|----------|--------|
| **Claude Code** | `~/.claude/projects/**/*.jsonl` | Actual Anthropic API counts |
| **GitHub Copilot Chat** | VS Code `workspaceStorage/<hash>/chatSessions/` (and `GitHub.copilot*/{chatSessions,transcripts,debug-logs}`, `globalStorage/emptyWindowChatSessions`) | Actual when present, else estimated |

VS Code variants scanned: Code, Code - Insiders, Code - Exploration, VSCodium, Cursor
(plus `.vscode-server` remotes).

### How each column is computed (mirrors the extension)

| Column | Claude Code | Copilot Chat |
|--------|-------------|--------------|
| Turns | `countClaudeCodeInteractions` — real user text turns | requests with non-empty `message.text` |
| Tools | non-MCP `tool_use` blocks | non-MCP `toolInvocationSerialized`/`prepareToolInvocation` + 1 per request with an `agent.id` |
| Input / Output | `message.usage` (incl. cache), deduped by `message.id` | **actual** `result.metadata.{promptTokens,outputTokens}` when present, else `len(text) × ratio` estimate |
| Thinking | 0 (folded into output) | estimated from `thinking` response items |
| Cached | `cache_read_input_tokens` | 0 (delta sessions expose none) |
| Total | input + output | actual sum, else input + output + thinking |
| Cost | `calculateEstimatedCost` × `modelPricing.json` | same; unknown models (`auto`) fall back to `gpt-4o-mini` rates |

These were validated against the extension's own output — e.g. the "TikTok extraction
error report" Copilot session reproduces Turns=5, Tools=32, Input=285,600, Output=3,072,
Thinking=14,545, Total=288,672, Cost=$0.0447 exactly.

Pricing comes from [`vscode-extension/src/modelPricing.json`](../vscode-extension/src/modelPricing.json)
and estimator ratios from `tokenEstimators.json` when found next to the repo; small
built-in fallbacks are used otherwise.

## Files / exporting

This folder is **self-contained** — zip it and send the whole folder. Required files:

```
session-insights/
├── session_insights.py    # the program
├── modelPricing.json      # cost rates (used for the Cost column)
├── tokenEstimators.json   # char→token ratios (Copilot token estimation)
└── README.md              # this file (optional)
```

Only `session_insights.py` is strictly required to run; without the two JSON files it
still works but uses small built-in fallback tables (less accurate Cost for uncommon
models). The program reads each recipient's **own** local Claude Code / Copilot Chat
session files, so your friend sees their own sessions, not yours.

No installation, virtualenv, or `pip install` needed — **Python 3.8+ only**.

## Usage

Requires Python 3.8+ (standard library only).

```bash
python session_insights.py                 # today's sessions
python session_insights.py --date 2026-06-10
python session_insights.py --days 7        # active in the last 7 days
python session_insights.py --all           # every session
python session_insights.py --sort totalTokens   # sort by another column
python session_insights.py --asc           # ascending order
python session_insights.py --json          # machine-readable JSON
```

Sort columns: `title`, `interactions`, `toolCalls`, `inputTokens`, `outputTokens`,
`thinkingTokens`, `cachedTokens`, `totalTokens`, `estimatedCost`, `editor`, `lastActivity`.

### `--pr` — which session produced this PR?

After you commit and push a feature, run this from the repo to find the session(s)
behind it. It correlates each session's edited/read files, git branch, and timing
against the change set, then prints the **same table** (token usage + cost) filtered
to the matching session(s), best match first.

```bash
python session_insights.py --pr            # current branch's diff vs main
python session_insights.py --pr 1401       # GitHub PR #1401 (needs `gh` installed + auth)
python session_insights.py --pr my-branch  # a branch
python session_insights.py --pr d668bf49   # a commit
python session_insights.py --pr --top 3    # show the top 3 candidates
python session_insights.py --pr --json     # adds match_score + matched files per session
```

Scoring: edited PR file ×5, read PR file ×1, branch match +6, activity within 48h of the
commit 0–3. Only sessions whose working directory is inside the repo are considered.
Works best on a feature branch (branch + file signals both fire); on `main` it falls back
to your last commit's diff and relies on file overlap.

📖 **Step-by-step walkthrough** (branch → build with the agent → commit → push PR → match):
see [USING-PR-MODE.md](USING-PR-MODE.md).

**Both Claude Code _and_ Copilot Chat sessions are matched.** The repo is resolved per
source — Claude via each event's `cwd`/`gitBranch`, Copilot via its workspace's
`workspace.json` folder. File overlap uses the files a session edited (`textEditGroup`
edits for Copilot) and referenced. Branch-match only applies to Claude (Copilot sessions
don't record a git branch), so Copilot relies on file overlap + timing.

## Notes

- Sessions with zero user interactions are skipped, matching the extension.
- "Today" is determined in your **local** timezone from each session's last activity.
- Copilot token counts are **estimated** (`chars × ~0.25`) only when a session has no
  actual `result.metadata` token data; otherwise the real counts are used.
- Live sessions change between runs — numbers for an in-progress session will keep
  growing as you use it (re-run to refresh).
- Not reimplemented: Copilot **CLI**, Gemini, OpenCode, Cursor-native, and other adapters
  the extension also aggregates. Only Claude Code and Copilot Chat are covered here.
