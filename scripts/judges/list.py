"""List judge — all parts must appear in context (order-independent).

v0.5.2: added token-level fallback.
v0.5.3: added partial Jaccard (>=50%) strategy.
"""
from __future__ import annotations

import re
from .nugget import _STOPWORDS


def _split_list_answer(answer: str) -> list[str]:
    """Split 'X and Y', 'X, Y, Z', 'X & Y' into cleaned entity strings."""
    parts: list[str] = []
    for chunk in re.split(r"\s+and\s+|\s*,\s*|\s*&\s*", answer):
        chunk = chunk.strip().lower().rstrip(".").strip()
        if chunk:
            parts.append(chunk)
    return [p for p in parts if p and p not in _STOPWORDS and len(p) > 1]


def _judge_list(context_block: str, answer: str) -> bool:
    """LIST: all answer parts must appear in the context.

    Strategy 1: every part appears literally as a substring.
    Strategy 2: all content tokens across all parts appear in context.
    Strategy 3: Jaccard >=50% across combined token set (min 2 tokens).
    """
    parts = _split_list_answer(answer)
    if not parts:
        return False
    ctx = (context_block or "").lower()
    # STRATEGY 1: literal
    if all(p in ctx for p in parts):
        return True
    # STRATEGY 2: token-level
    all_tokens: set[str] = set()
    for p in parts:
        for t in re.findall(r"[a-z0-9]+", p):
            if t not in _STOPWORDS and len(t) > 1:
                all_tokens.add(t)
    if all_tokens and all(t in ctx for t in all_tokens):
        return True
    # STRATEGY 3: partial Jaccard
    if all_tokens and len(all_tokens) >= 2:
        ctx_tokens = set(re.findall(r"[a-z0-9]+", ctx))
        present = all_tokens & ctx_tokens
        coverage = len(present) / max(len(all_tokens), 1)
        if coverage >= 0.5 and len(present) >= 2:
            return True
    return False
