# Positive Use Case — Guardrailed Support Assistant

A small, deterministic FAQ assistant that demonstrates the kind of AI-adjacent
change AEI is designed to **APPROVE**: real product value, a strong safety
posture, and zero governance risk.

## Why AEI approves this change

| Dimension | What this change does | Result |
|---|---|---|
| 🟢 Quality | Answers come from a fixed, reviewed knowledge base, so responses stay aligned with the support benchmark. | Stable |
| 🟢 Cost | Lookups are local string matching with no model call — effectively free to serve. | No regression |
| 🟢 Governance | No personal data embedded, no broad permissions taken, and the system prompt only **adds** guardrails. | Clean |

The module reads a JSON knowledge base and matches text. It does not run dynamic
code, take broad filesystem access, or echo secrets — exactly the profile a
release gate wants to wave through.

## Files

- `support_assistant.py` — the assistant: knowledge-base loader, matcher, and CLI.
- `knowledge_base.json` — the reviewed answers the assistant is allowed to give.

## Usage

```bash
python "support_assistant.py" "How do I reset my password?"
python "support_assistant.py" --show-prompt
```

Anything outside the published material gets a polite hand-off to a human agent
rather than a guess.
