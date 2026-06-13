# Finding the session behind a PR — step by step

`python session_insights.py --pr` answers one question:

> "I just shipped a PR with an AI chat agent. **Which session produced it, and how
> many tokens / how much did it cost?**"

It works for both **Claude Code** and **GitHub Copilot Chat** sessions. This guide walks
the whole flow, from opening the agent to reading the final report.

---

## Step 0 — One-time setup

- **Python 3.8+** (standard library only — nothing to `pip install`).
- Run the commands below **from inside your repo** (the matcher uses the repo's git data).
- *Optional:* install the **GitHub CLI** (`gh`) and run `gh auth login`. This is only
  needed if you want to pass a **PR number** later. Everything else works with plain git.

---

## Step 1 — Start a feature branch

Create a branch so the work is isolated. (The agent records the branch name, which becomes
a strong matching signal for Claude Code sessions.)

```bash
git checkout -b feat/my-feature
```

---

## Step 2 — Build the feature with your chat agent

Open **Claude Code** or **Copilot Chat** in the repo and let the agent do the work — ask it
to implement the feature, edit files, run tests, etc.

> 💡 The agent must actually **edit files** for matching to work. Every `Edit`/`Write`
> (Claude) or applied edit (Copilot) is logged locally and is what `--pr` correlates
> against your PR's changed files. A pure "ask a question" chat that never touches files
> can't be matched by file overlap.

When you're done, you'll have local changes for the feature.

---

## Step 3 — Review and commit

```bash
git add -A
git commit -m "feat: add my feature"
```

---

## Step 4 — Push and open the PR

```bash
git push -u origin feat/my-feature
```

Then open the PR — either in the GitHub UI, or with the CLI:

```bash
gh pr create --fill
```

---

## Step 5 — Find the session that produced the PR

Run this **from the repo, while still on the feature branch**:

```bash
python session_insights.py --pr
```

With no argument, `--pr` compares **your current branch against `main`** to get the list of
changed files, then ranks every local session by how well it matches.

### Other ways to point it at the PR

```bash
python session_insights.py --pr 1401          # a GitHub PR number (needs gh installed + auth)
python session_insights.py --pr feat/my-feature   # a specific branch
python session_insights.py --pr d668bf49      # a specific commit
```

---

## Step 6 — Read the report

You get the **same table as `python session_insights.py`** — full token usage and cost —
but filtered to the matching session(s), best match first:

```
🔎 Sessions for: current branch feat/my-feature vs main
   Changed files (3): src/feature.ts, src/feature.test.ts, README.md
   Ranked by match confidence (best match first).

#  Title                         Turns  Tools    Input  Output  Cached    Total      Cost  Editor       Models           Last Active
1  Add my feature                   12    140  2,310,55  41,002  2,180,3  2,351,557  $12.10  Claude Code  Claude Opus 4.8        14:22

Sessions: 1   Turns: 12   Tools: 140   Total tokens: 2,351,557   Cost: $12.10

  1. Add my feature — edited 3 PR file(s), branch match ⭐ (score 24.0)
```

- The **table** is the per-session breakdown (Turns, Tools, Input, Output, Thinking,
  Cached, Total, **Cost**, Editor, Models, Last Active).
- The **totals line** sums the matched session(s).
- The **footnote** explains *why* each session matched (how many PR files it edited/read,
  whether the branch matched, and the score).

### See more than the top match

If several sessions touched the PR's files (e.g. you split the work across chats), show
them all and compare:

```bash
python session_insights.py --pr --top 5
```

### Machine-readable output

```bash
python session_insights.py --pr --json
```

The JSON adds `match_score`, `branch_match`, `matched_edited_files`, and
`matched_read_files` to each session, alongside the normal token fields.

---

## How a session gets matched (the scoring)

| Signal | Points | Notes |
|--------|-------:|-------|
| Each PR file the session **edited** | ×5 | The strongest fingerprint |
| Each PR file the session **read** | ×1 | Supporting evidence |
| Session's git branch == PR branch | +6 | **Claude Code only** (Copilot records no branch) |
| Active within 48h of the commit | 0–3 | Fades with time; tie-breaker |

Only sessions whose working directory / workspace is **inside this repo** are considered.
The top row is almost always the session that actually produced the PR.

| Source | Repo detected via | Edited files via |
|--------|-------------------|------------------|
| **Claude Code** | each event's `cwd` + `gitBranch` | `Edit` / `Write` / `MultiEdit` tool calls |
| **Copilot Chat** | the workspace's `workspace.json` folder | applied `textEditGroup` edits |

---

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `No local session … touched those files` | The agent edited files in a **different repo**, or it was a no-edit "ask" chat, or the session files were cleared. |
| `Could not resolve PR #N via gh …` | `gh` isn't installed/authenticated, or the number is wrong. Use `--pr <branch>` or `--pr <commit>` instead. |
| Falls back to "last commit on main" | You ran it **on `main`** with nothing ahead. Run it on the feature branch, or pass an explicit branch/commit/PR. |
| Branch didn't boost the score (Copilot) | Expected — Copilot sessions don't record a git branch; they match on file overlap + timing. |
| Numbers keep changing between runs | An in-progress session grows as you use it. Re-run after the work is finished. |

---

## TL;DR

```bash
git checkout -b feat/my-feature        # 1. branch
# 2. build it with Claude Code / Copilot Chat (let it edit files)
git add -A && git commit -m "feat: …"  # 3. commit
git push -u origin feat/my-feature     # 4. push + open PR
python session_insights.py --pr        # 5. which session made it — tokens & cost
```
