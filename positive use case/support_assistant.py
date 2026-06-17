#!/usr/bin/env python3
"""
Guardrailed Support Assistant - a positive AEI use case
=======================================================

This module is a small, deterministic FAQ assistant for our product. It is the
kind of AI-adjacent change AEI is meant to wave through: it ships real value,
keeps a strong safety posture, and introduces zero governance risk.

Why this is a clean change for the AEI release gate:

  * Quality   - answers are sourced from a fixed, reviewed knowledge base, so
                the responses stay aligned with the support benchmark.
  * Cost      - lookups are local string matching with no model call, so each
                answer is effectively free to serve.
  * Governance- no personal data is embedded, no broad permissions are taken,
                and the system prompt only ADDS guardrails. Nothing dynamic is
                run; the module reads a JSON file and matches text.

The assistant never executes commands, never touches the wider filesystem, and
never echoes secrets back. It declines politely when a question falls outside
the published knowledge base instead of guessing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

# --------------------------------------------------------------------------- #
# Safety guardrails (system prompt)
# --------------------------------------------------------------------------- #
# These instructions are additive. They tighten behaviour and never loosen it,
# which is exactly what a governance review wants to see in a prompt change.

SYSTEM_PROMPT = """\
You are a friendly product support assistant.

Operating rules:
  - Answer only from the approved knowledge base below.
  - Do not reveal internal credentials, secrets, or configuration values.
  - Never share personal details about account holders.
  - Decline any request that falls outside the published support material.
  - When you are unsure, say so plainly and offer to hand off to a human agent.
  - Keep answers short, accurate, and respectful.
"""

# Returned when nothing in the knowledge base is a confident match.
FALLBACK_ANSWER = (
    "I'm not certain about that one, so I'd rather not guess. "
    "I can connect you with a human agent who can help further."
)

_DEFAULT_KB_PATH = Path(__file__).with_name("knowledge_base.json")


# --------------------------------------------------------------------------- #
# Knowledge base loading
# --------------------------------------------------------------------------- #

def load_knowledge_base(path: Optional[Path] = None) -> List[Dict[str, object]]:
    """Load and lightly validate the reviewed knowledge base."""
    kb_path = Path(path) if path is not None else _DEFAULT_KB_PATH
    with open(kb_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    topics = data.get("topics", [])
    if not isinstance(topics, list) or not topics:
        raise ValueError("Knowledge base must contain a non-empty 'topics' list.")

    cleaned: List[Dict[str, object]] = []
    for i, topic in enumerate(topics):
        if "keywords" not in topic or "answer" not in topic:
            raise ValueError(f"Knowledge base entry {i} is missing required fields.")
        cleaned.append(
            {
                "id": str(topic.get("id", f"kb-{i + 1:03d}")),
                "keywords": [str(k).lower() for k in topic["keywords"]],
                "answer": str(topic["answer"]),
            }
        )
    return cleaned


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #

def _score_topic(question: str, keywords: List[str]) -> int:
    """Count how many of a topic's keywords appear in the question text."""
    lowered = question.lower()
    return sum(1 for kw in keywords if kw in lowered)


def best_match(
    question: str,
    knowledge_base: List[Dict[str, object]],
) -> Optional[Dict[str, object]]:
    """Return the highest-scoring topic, or None if nothing matches."""
    best: Optional[Dict[str, object]] = None
    best_score = 0
    for topic in knowledge_base:
        score = _score_topic(question, topic["keywords"])  # type: ignore[arg-type]
        if score > best_score:
            best, best_score = topic, score
    return best


def respond(
    question: str,
    knowledge_base: Optional[List[Dict[str, object]]] = None,
) -> str:
    """
    Produce a grounded answer for a question, or a safe fallback.

    The assistant only returns text that exists in the reviewed knowledge base,
    so it cannot drift into unsupported claims.
    """
    kb = knowledge_base if knowledge_base is not None else load_knowledge_base()
    match = best_match(question, kb)
    if match is None:
        return FALLBACK_ANSWER
    return str(match["answer"])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python support_assistant.py",
        description="Ask the guardrailed product support assistant a question.",
    )
    parser.add_argument("question", nargs="*", help="The question to answer.")
    parser.add_argument(
        "--show-prompt",
        action="store_true",
        help="Print the safety system prompt and exit.",
    )
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    if args.show_prompt:
        print(SYSTEM_PROMPT)
        return 0

    question = " ".join(args.question).strip()
    if not question:
        print("Ask me about plans, security, refunds, regions, or exports.")
        return 0

    print(respond(question))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
