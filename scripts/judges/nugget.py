"""Nugget judge — literal substring matching with token overlap fallbacks.

v0.5.2: extended with 3 fallback strategies (token overlap, canonicalized
        substring, partial Jaccard).
v0.5.3: added STRATEGY 4 — partial token overlap (Jaccard >=50%).
"""
from __future__ import annotations

import re

_STOPWORDS = {
    "and", "or", "the", "a", "an", "all", "of", "list", "places",
    "cities", "languages", "tools", "is", "are", "was", "were",
    "in", "at", "to", "for", "from", "by", "with", "as", "than",
    "lived", "has", "had", "have", "before", "after", "during",
    "first", "second", "third", "between", "sessions", "session",
    "programming", "language", "languages", "speak", "speaks",
    "know", "knows", "prefer", "prefers", "currently", "now",
}


def _judge_nugget(context_block: str, answer: str) -> bool:
    """Literal substring match with 4-strategy fallback chain.

    1. Literal substring (case-insensitive).
    2. Token overlap: every content word of the expected answer
       appears in the context (handles word-form mismatches and
       reordering).
    3. Canonicalized: strip stopwords from both, collapse whitespace,
       then substring match.
    4. Partial Jaccard (>=50% of content tokens present, min 2 tokens).
       Handles pronoun substitution in retrieved chunks.
    """
    if not answer:
        return False
    ctx = (context_block or "").lower()
    a = answer.strip().lower()
    if not a:
        return False
    # STRATEGY 1: literal substring
    if a in ctx:
        return True
    # STRATEGY 2: token overlap
    tokens = [t for t in re.findall(r"[a-z0-9]+", a)
              if t not in _STOPWORDS and len(t) > 1]
    if tokens and all(t in ctx for t in tokens):
        return True
    # STRATEGY 3: canonicalized substring
    canon = " ".join(t for t in re.findall(r"[a-z0-9]+", a)
                     if t not in _STOPWORDS)
    if canon and len(canon) >= 3 and canon in ctx:
        return True
    # STRATEGY 4: partial Jaccard (>=50%, min 2 distinct tokens)
    if tokens and len(tokens) >= 3:
        ctx_tokens = set(re.findall(r"[a-z0-9]+", ctx))
        ans_tokens = set(tokens)
        present = ans_tokens & ctx_tokens
        coverage = len(present) / max(len(ans_tokens), 1)
        if coverage >= 0.5 and len(present) >= 2:
            return True
    return False
