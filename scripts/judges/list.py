"""List judge — all parts must appear in context (order-independent)."""
from __future__ import annotations

import re


def _judge_list(context_block: str, answer: str) -> bool:
    """LIST: answer contains ' and ' / ', ' → all parts must appear."""
    cb = (context_block or "").lower()
    a = (answer or "").strip().lower()
    if not a:
        return False
    parts = re.split(r"\s+and\s+|\s*,\s+|\s*&\s+", a)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) < 2:
        return False
    return all(p in cb for p in parts)
