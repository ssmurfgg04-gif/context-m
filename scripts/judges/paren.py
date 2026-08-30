"""Parenthetical abbreviation judge."""
from __future__ import annotations

import re


def _judge_paren_abbreviation(context_block: str, answer: str) -> bool:
    """Paren-abbreviation: 'UCLA' in context matches 'University of California, Los Angeles (UCLA)'"""
    cb = (context_block or "").lower()
    a = (answer or "").strip().lower()
    if not a:
        return False
    m = re.search(r"\(([^)]+)\)", a)
    if not m:
        return False
    abbrev = m.group(1).lower()
    if len(abbrev) < 2:
        return False
    return abbrev in cb
