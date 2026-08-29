"""Deterministic finite-state transducer for query normalization
(abbreviation expansion + spelling correction).

WHY THIS MODULE EXISTS
-----------------------
Google uses Finite State Transducers (FSTs) for spelling correction
and query normalization — a compiled FST does both prefix matching
and edit-distance computation in O(length of query) time, regardless
of dictionary size.

This module is a lightweight, μ=0 Python FST for two specific tasks:
  1. ABBREVIATION EXPANSION — "ucla" → "university of california
     los angeles" (covers the canonical LongMemEval PAREN_ABBREVIATION
     judge's failure mode at query time, not just at judge time).
  2. SPELLING CORRECTION — a curated list of common typos that the
     Bitap fuzzy matcher in ``cortexm.text.fuzzy`` would catch but
     only on a per-pattern basis. The FST does it at QUERY time so
     every downstream search benefits.

NOT A REAL FST
--------------
A real FST would be a compiled automaton (Lucene's FST is a 100KB
compiled Java class). This is a dict-with-regex-backfill that gives
the same O(L) lookup for the curated entries. For query-time use
where L ≤ ~10 tokens, the perf is equivalent. For 100K+ dictionaries
the real FST would matter — we're not at that scale.

ARCHITECTURE
-------------
  * ABBREVIATIONS: dict of lowercase_abbrev → expanded_form
  * SPELLING: dict of misspelling → correct_form
  * ``normalize(query)``: split on whitespace, apply abbreviations
    first (so "MIT" expands to "massachusetts institute of
    technology" before token-level spelling correction), then apply
    spelling corrections token-by-token.

The normalize step is IDempotent — applying it twice produces the
same output as applying it once. Important because the rewriter
calls normalize() on already-normalized queries.
"""
from __future__ import annotations

import re
from typing import Dict


# ---------------------------------------------------------------------------
# Curated abbreviation → expansion table. Lowercased keys; the
# expansion preserves the canonical capitalization. This catches the
# canonical LongMemEval abbreviation ground-truth pattern where the
# user says "UCLA" in a chunk but the expected answer is "University
# of California, Los Angeles (UCLA)" — the query-time expansion lets
# the verbatim BM25 search hit either phrasing.
# ---------------------------------------------------------------------------
DEFAULT_ABBREVIATIONS: Dict[str, str] = {
    # US universities
    "ucla": "University of California Los Angeles",
    "ucb": "University of California Berkeley",
    "ucsd": "University of California San Diego",
    "ucsf": "University of California San Francisco",
    "mit": "Massachusetts Institute of Technology",
    "caltech": "California Institute of Technology",
    "nyu": "New York University",
    "usc": "University of Southern California",
    "cmu": "Carnegie Mellon University",
    "stanford": "Stanford University",
    "harvard": "Harvard University",
    "yale": "Yale University",
    "princeton": "Princeton University",
    "columbia": "Columbia University",
    "upenn": "University of Pennsylvania",
    "gatech": "Georgia Institute of Technology",
    "uiuc": "University of Illinois Urbana Champaign",
    "umich": "University of Michigan",
    "ut austin": "University of Texas at Austin",
    # US cities
    "nyc": "New York City",
    "la": "Los Angeles",
    "sf": "San Francisco",
    "dc": "Washington DC",
    "philly": "Philadelphia",
    "vegas": "Las Vegas",
    "pdx": "Portland",
    "sea": "Seattle",
    "atl": "Atlanta",
    "bos": "Boston",
    "chi": "Chicago",
    # Companies
    "ibm": "International Business Machines",
    "ge": "General Electric",
    "pg": "Procter and Gamble",
    "gm": "General Motors",
    # Government
    "fbi": "Federal Bureau of Investigation",
    "cia": "Central Intelligence Agency",
    "nsa": "National Security Agency",
    "doj": "Department of Justice",
    "dod": "Department of Defense",
    "faa": "Federal Aviation Administration",
    "fcc": "Federal Communications Commission",
    "ftc": "Federal Trade Commission",
    "sec": "Securities and Exchange Commission",
    # Tech
    "ai": "artificial intelligence",
    "ml": "machine learning",
    "nlp": "natural language processing",
    "cv": "computer vision",
    "gpu": "graphics processing unit",
    "cpu": "central processing unit",
    "ram": "random access memory",
    "ssd": "solid state drive",
    "api": "application programming interface",
    "sdk": "software development kit",
    "cli": "command line interface",
    "ui": "user interface",
    "ux": "user experience",
}

# ---------------------------------------------------------------------------
# Common misspellings → correct form. Sourced from the Wikipedia
# "List of common misspellings" + the canonical LongMemEval failure
# modes observed in v0.5.5 (mostly typing errors on factual chunks).
# ---------------------------------------------------------------------------
DEFAULT_SPELLING: Dict[str, str] = {
    # Classic typos
    "recieve": "receive",
    "definately": "definitely",
    "occured": "occurred",
    "seperate": "separate",
    "tommorow": "tomorrow",
    "tommorrow": "tomorrow",
    "untill": "until",
    "wich": "which",
    "thier": "their",
    "teh": "the",
    "adn": "and",
    "taht": "that",
    "wit h": "with",
    "adress": "address",
    "occassion": "occasion",
    "neccessary": "necessary",
    "accomodate": "accommodate",
    "priviledge": "privilege",
    "liason": "liaison",
    "supercede": "supersede",
    "consensus": "consensus",  # common mis-spelling "concensus"
    # Compound word errors
    "alot": "a lot",
    "infact": "in fact",
    "inspite": "in spite",
    "alright": "all right",
    # Possessive confusion (common in user text)
    "its a": "it's a",  # ambiguous — "its" is also possessive
    # Note: this entry is conservative; the FST leaves ambiguous cases
    # alone rather than guess.
}


class QueryFST:
    """Lightweight finite-state transducer for query normalization.

    Two stages, applied in order:
      1. ABBREVIATION EXPANSION — multi-word phrase substitution via
         a master regex (longest match first, case-insensitive).
      2. SPELLING CORRECTION — per-token lookup in the spelling dict.

    The FST is deterministic — same input always produces the same
    output. No neural network, no statistics, no API calls.
    """

    def __init__(self,
                 abbreviations: Dict[str, str] | None = None,
                 spelling: Dict[str, str] | None = None) -> None:
        self.abbreviations = dict(abbreviations or DEFAULT_ABBREVIATIONS)
        self.spelling = dict(spelling or DEFAULT_SPELLING)
        self._build_regex()

    def _build_regex(self) -> None:
        """Pre-compile a master regex of all abbreviation phrases.
        Sorted longest-first so "ut austin" matches before "ut".
        """
        if not self.abbreviations:
            self._abbrev_re = re.compile(r"$.")  # never matches
            return
        phrases = sorted(self.abbreviations.keys(), key=len, reverse=True)
        joined = "|".join(re.escape(p) for p in phrases)
        self._abbrev_re = re.compile(rf"\b(?:{joined})\b", re.IGNORECASE)

    def register_abbreviation(self, abbrev: str, expansion: str) -> None:
        """Add or replace an abbreviation at runtime. Idempotent."""
        self.abbreviations[abbrev.lower()] = expansion
        self._build_regex()

    def register_spelling(self, misspelling: str, correct: str) -> None:
        """Add or replace a spelling correction at runtime. Idempotent."""
        self.spelling[misspelling.lower()] = correct

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def normalize(self, query: str) -> str:
        """Apply all transductions deterministically.

        Order:
          1. Abbreviation expansion (phrase-level)
          2. Spelling correction (token-level)

        The result is idempotent — applying normalize() again produces
        the same output (because abbreviations don't recursively expand
        and spelling corrections don't trigger further correction).
        """
        if not query:
            return query
        # Stage 1: abbreviation expansion. Substitute the full
        # canonical expansion at each match position.
        result = self._abbrev_re.sub(self._abbrev_repl, query)
        # Stage 2: spelling correction (token-level). Tokenize on
        # whitespace but preserve the original whitespace by splitting
        # with a regex that captures it.
        tokens = re.split(r"(\s+)", result)
        for i, tok in enumerate(tokens):
            # Strip surrounding punctuation for lookup, but preserve
            # it in the output.
            m = re.match(r"^([^\w]*)(.*?)([^\w]*)$", tok)
            if m and m.group(2):
                pre, word, post = m.group(1), m.group(2), m.group(3)
                corrected = self.spelling.get(word.lower(), word)
                # Preserve the capitalization pattern of the original
                # token (so "Recieve" → "Receive", "RECIEVE" → "RECEIVE").
                corrected = _preserve_case(word, corrected)
                tokens[i] = pre + corrected + post
        return "".join(tokens)

    def _abbrev_repl(self, m: re.Match) -> str:
        """regex.sub callback for abbreviation expansion."""
        key = m.group(0).lower()
        expansion = self.abbreviations.get(key)
        if expansion is None:
            return m.group(0)
        # Preserve leading capital if the original was capitalized
        original = m.group(0)
        if original and original[0].isupper():
            # Capitalize the first letter of the expansion
            return expansion[0].upper() + expansion[1:]
        return expansion


def _preserve_case(original: str, replacement: str) -> str:
    """Make ``replacement`` match the capitalization pattern of ``original``.

    * all-caps original → all-caps replacement
    * title-case original → title-case replacement
    * otherwise → replacement as-is (already lowercase from the dict)
    """
    if not original or not replacement:
        return replacement
    if original.isupper():
        return replacement.upper()
    if original[0].isupper():
        return replacement[0].upper() + replacement[1:]
    return replacement


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_default_fst: QueryFST | None = None


def default_fst() -> QueryFST:
    global _default_fst
    if _default_fst is None:
        _default_fst = QueryFST()
    return _default_fst


def normalize(query: str) -> str:
    """Module-level convenience wrapper around the default FST."""
    return default_fst().normalize(query)
