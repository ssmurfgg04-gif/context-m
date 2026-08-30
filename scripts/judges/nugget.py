"""Nugget judge — literal substring matching with token overlap fallbacks."""
from __future__ import annotations

import re


def _judge_nugget(context_block: str, answer: str) -> bool:
    """Literal substring match with token-overlap fallbacks."""
    cb = (context_block or "").lower()
    a = (answer or "").strip().lower()
    if not a:
        return False
    if a in cb:
        return True
    # Token overlap fallback
    a_tokens = set(a.split())
    cb_tokens = set(cb.split())
    if len(a_tokens) >= 2 and len(a_tokens & cb_tokens) == len(a_tokens):
        return True
    return False
