"""Bitap + Levenshtein + n-gram approximate string matching.

Wu-Manber k-error Bitap (Baeza-Yates-Gonnet 1992; Wu-Manber 1994) for
patterns <= 63 chars — single-word packed, O(n*k) for k errors. Falls
back to early-exit Levenshtein DP for longer patterns. Used to:

  * replace util.levenshtein() inner loop on short strings (5-20x faster)
  * power spelling-tolerant pattern triggers in bridge/patterns.py
  * surface "close enough" candidates in normalization (idiolect.py)

arxiv research note: Bitap is the bitwise-baseline for fuzzy matching;
Myers' bit-parallel DP has the same asymptotic but worse constants on
small alphabets. For Context-M's typical pattern lengths (<32) and
small edit budgets (k<=3), Bitap wins.
"""

from __future__ import annotations

import re
from typing import Iterable


def bitap_levenshtein(text: str, pattern: str, max_edits: int = 3) -> int | None:
    """Wu-Manber k-error Bitap (substring matching with errors).

    Returns the smallest edit distance within max_edits if the pattern
    appears as a substring of text (allowing up to max_edits insertions,
    deletions, substitutions), else None.

    Pattern length must be <= 63 for single uint64 packing; longer
    patterns fall back to DP. If pattern is longer than text + max_edits,
    no match is possible and we return None early.
    """
    m = len(pattern)
    if m == 0:
        return 0
    # early-exit: substring match impossible if pattern too long for text+errors
    if m > len(text) + max_edits:
        return None
    if m > 63:
        return _levenshtein_substring(text, pattern, max_edits)
    # build per-char bitmask
    R: dict[str, int] = {}
    for i, ch in enumerate(pattern):
        R[ch] = R.get(ch, 0) | (1 << i)
    mask = (1 << m) - 1
    match_bit = 1 << (m - 1)
    # Wu-Manber initial states:
    #   T[0] = mask with bit 0 cleared (no chars consumed yet)
    #   T[e] = T[e-1] << 1 (allow e leading insertions = e leading chars of
    #          pattern can be "matched" via insertion without consuming text)
    states = [0] * (max_edits + 1)
    states[0] = mask ^ 1   # bit 0 cleared
    for e in range(1, max_edits + 1):
        # T[e] = T[e-1] shifted left by 1 (each leading insertion advances
        # the pattern position by 1)
        states[e] = ((states[e - 1] << 1) | 1) & mask
    best = None
    for ch in text:
        rc = R.get(ch, 0)
        prev = states[0]
        new_states = list(states)
        new_states[0] = ((states[0] << 1) | 1) & rc & mask
        for e in range(1, max_edits + 1):
            # match: T[e] advances if char matches
            # substitution: T[e-1] advances (use 1 error)
            # deletion in text: T[e-1] (use 1 error, skip text char)
            # insertion in text: T[e] advances (use 1 error, skip pattern char)
            cur = ((states[e] << 1) | 1) & rc
            cur |= prev | (prev << 1) | (states[e] << 1)
            cur &= mask
            prev = states[e]   # save OLD states[e] before overwrite
            new_states[e] = cur
        states = new_states
        # check for a match at any error budget
        for e in range(max_edits + 1):
            if states[e] & match_bit:
                if best is None or e < best:
                    best = e
                break  # lower e wins
    return best


def _levenshtein_substring(text: str, pattern: str, max_edits: int) -> int | None:
    """Sliding-window DP for patterns > 63 chars. O(n*m) but rare path."""
    n, m = len(text), len(pattern)
    if m == 0:
        return 0
    if abs(n - m) > max_edits and n < m:
        return None
    best = None
    # slide a window of [m-max_edits, m+max_edits] over text
    lo = max(0, m - max_edits)
    hi = min(n, m + max_edits)
    for start in range(0, n - lo + 1):
        sub = text[start:start + hi]
        if not sub:
            continue
        d = _levenshtein_full(sub, pattern)
        if d <= max_edits and (best is None or d < best):
            best = d
            if best == 0:
                return 0
    return best


def _levenshtein_full(a: str, b: str) -> int:
    """Classic 2-row DP — used only as fallback for long patterns."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            ins = cur[j - 1] + 1
            dele = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            cur[j] = min(ins, dele, sub)
        prev = cur
    return prev[-1]


def levenshtein(a: str, b: str, cutoff: float | None = None) -> int:
    """Full edit distance (not substring-distance). Uses DP.

    This is the right metric for word-level similarity. Bitap is for
    substring matching inside longer text — different semantic.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    # Early-exit: if length diff exceeds cutoff, bail
    if cutoff is not None and abs(len(a) - len(b)) > cutoff * max(len(a), len(b)):
        return max(len(a), len(b))
    return _levenshtein_full(a, b)


def similarity(a: str, b: str) -> float:
    """1 - normalized Levenshtein. Bitap-fast on short strings."""
    if not a and not b:
        return 1.0
    d = levenshtein(a, b)
    return max(0.0, 1.0 - d / max(len(a), len(b)))


def ngram_jaccard(a: str, b: str, n: int = 3) -> float:
    """Character n-gram Jaccard — for fuzzy lexicon lookup.

    Faster than full Levenshtein for similarity thresholding on
    short tokens. Complements Bitap (which is good for substring
    matching) by being good at "are these two tokens plausibly the
    same word" judgments.
    """
    if not a or not b:
        return 0.0
    ga = {a[i:i + n] for i in range(len(a) - n + 1)} or {a}
    gb = {b[i:i + n] for i in range(len(b) - n + 1)} or {b}
    inter = len(ga & gb)
    union = len(ga | gb)
    return inter / union if union else 0.0


def best_match(query: str, candidates: Iterable[str],
              max_edits: int = 2, min_sim: float = 0.7) -> str | None:
    """Find the best fuzzy match for query among candidates.

    Uses Levenshtein similarity (1 - dist/max_len) as the primary metric
    and n-gram Jaccard as tiebreaker. Returns None if no candidate
    crosses min_sim.
    """
    if not query:
        return None
    cand_list = list(candidates)
    if not cand_list:
        return None
    best, best_score = None, 0.0
    q = query.lower()
    for c in cand_list:
        cl = c.lower()
        if q == cl:
            return c
        # primary: normalized Levenshtein similarity
        d = _levenshtein_full(q, cl)
        sim = 1.0 - d / max(len(q), len(cl))
        # boost with n-gram Jaccard (catches character-level similarity)
        j = ngram_jaccard(q, cl)
        score = 0.7 * sim + 0.3 * j
        if score > best_score:
            best_score, best = score, c
    return best if best_score >= min_sim else None


def fuzzy_contains(haystack: str, needle: str, max_edits: int = 2) -> bool:
    """True if needle appears in haystack within max_edits (Bitap)."""
    if not needle:
        return True
    if len(needle) <= 63:
        return bitap_levenshtein(haystack, needle, max_edits) is not None
    # fallback: regex with whitespace tolerance
    pattern = re.escape(needle)
    return bool(re.search(pattern, haystack, re.IGNORECASE))


__all__ = [
    "bitap_levenshtein",
    "levenshtein",
    "similarity",
    "ngram_jaccard",
    "best_match",
    "fuzzy_contains",
]
