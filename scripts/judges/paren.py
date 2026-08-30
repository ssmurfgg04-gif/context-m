"""Parenthetical abbreviation judge.

Acceptance criteria (all must hold):
  1. Answer contains a parenthetical: "X (ABBR)".
  2. ABBR is a 2-8 char alphanumeric token (typical academic/org abbreviation).
  3. ABBR appears in the context_block as a standalone word-boundary token
     (case-insensitive). Word-boundary match prevents "MIT" matching "submit".
"""
from __future__ import annotations

import re


def _judge_paren_abbreviation(context_block: str, answer: str) -> bool:
    if not answer:
        return False
    m = re.search(r"\(([A-Za-z][A-Za-z0-9]{1,7})\)", answer)
    if not m:
        return False
    abbr = m.group(1)
    cb = context_block or ""
    # Word-boundary match: "MIT" must not match inside "submit"
    return bool(re.search(rf"\b{re.escape(abbr)}\b", cb, re.I))
