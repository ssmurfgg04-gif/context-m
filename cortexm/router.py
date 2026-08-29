"""Query Router — heuristic tier selection (deterministic, no LLM).

Reddit deep-dive (2026-08-29): the system has TWO tiers now:

  * VERBATIM    — MemPalace-style FTS5 + dense over raw chunks.
                  Best for "where did I eat?" (factoid recall).
  * STRUCTURED  — bi-temporal Trace + VSA Palace over facts.
                  Best for "what changed since Jan?" (temporal).

Running BOTH tiers for every query is wasteful (2× cost) and
sometimes wrong (verbatim can dilute structured-only answers
with stale duplicates). The router picks the right tier(s).

This router is a 20-line heuristic. It costs nothing. It is
deterministic (preserves the "same every time" promise). It is
NOT an LLM.

Routing rules (priority order, first match wins):

  1. Temporal keywords → ['structured']
     "when", "before", "after", "changed", "now", "current",
     "used to", "previous", "since", "until", "became"
  2. Multi-hop / relation pattern → ['structured']
     "who introduced X to Y", "what is X's relation to Y"
     (look for subject-relation-object triple shapes)
  3. Exact-phrase clue → ['verbatim']
     Quoted strings ("Charlie"), named entities (Capitalized words
     that aren't sentence-initial), specific identifiers (PR #1234,
     CVE-2024-1234, IDs, version numbers)
  4. Default → ['verbatim', 'structured']
     Ambiguous queries hit both; the fusion bridge decides.

The router returns a list of tier names. The caller is responsible
for invoking each tier's search() and fusing results. This keeps
the router pure (no I/O, no side effects) and trivially testable.
"""
from __future__ import annotations

import re


# ---------------------------- rule 1: temporal -----------------------

TEMPORAL_KEYWORDS = frozenset({
    "when", "before", "after", "changed", "now", "current",
    "used to", "previous", "since", "until", "became", "was",
    "becomes", "transition", "switched", "moved", "updated",
    "version", "history", "timeline",
})


def _has_temporal_signal(query: str) -> bool:
    """True if the query contains temporal/reasoning keywords.

    These signal: the user wants to know about STATE CHANGE over
    time, which only the structured tier's bi-temporal Trace can
    answer.
    """
    q = query.lower()
    # Multi-word phrases first (longer match wins)
    for phrase in ("used to", "what changed", "since when",
                    "before this", "after this", "history of"):
        if phrase in q:
            return True
    # Single-word keywords (word-boundary match to avoid "now" in "know")
    for kw in TEMPORAL_KEYWORDS:
        if re.search(rf"\b{re.escape(kw)}\b", q):
            return True
    return False


# ---------------------------- rule 2: multi-hop ----------------------

# Subject-relation-object triple shape. Crude but effective:
#   "who introduced X to Y"
#   "what is X's relation to Y"
#   "how is X connected to Y"
MULTIHOP_PATTERNS = [
    re.compile(r"\bwho\s+\w+\s+\w+\s+to\b", re.I),
    re.compile(r"\bwhat(?:'s| is)?\s+\w+(?:'s)?\s+relation\s+to\b", re.I),
    re.compile(r"\bhow\s+(?:is|are)\s+\w+\s+connected\s+to\b", re.I),
    re.compile(r"\bpath\s+from\s+\w+\s+to\b", re.I),
]


def _has_multihop_signal(query: str) -> bool:
    """True if the query looks like a multi-hop relation question."""
    return any(rx.search(query) for rx in MULTIHOP_PATTERNS)


# ---------------------------- rule 3: exact-phrase --------------------

# Quoted strings: "Charlie", 'my dog'
QUOTED_STRING = re.compile(r"""['"]([^'"]{2,80})['"]""")

# Capitalized words that aren't sentence-initial (heuristic:
# appears after a non-terminal punctuation or in the middle)
MID_SENTENCE_CAPITALIZED = re.compile(
    r"(?:[.,;:!?]\s+|\s+)([A-Z][a-zA-Z]{2,})")

# Identifiers: PR #1234, CVE-2024-1234, version v1.2.3, etc.
IDENTIFIER = re.compile(
    r"\b(?:"
    r"PR\s*#\d+|"              # PR #1234
    r"issue\s*#\d+|"           # issue #5678
    r"CVE-\d{4}-\d{4,7}|"      # CVE-2024-12345
    r"v?\d+\.\d+(?:\.\d+)?|"   # v1.2.3
    r"[A-Z]{2,}-\d+|"          # JIRA-1234
    r"0x[0-9a-fA-F]+"          # 0xDEADBEEF
    r")\b")


def _has_exact_phrase_signal(query: str) -> bool:
    """True if the query contains a quoted string OR an identifier.

    These signal: the user wants EXACT-MATCH recall ("I told you
    'Charlie' last week, find it"). The verbatim tier's BM25 excels
    at this; the structured tier's VSA over facts often misses
    exact names because they get abstracted away in extraction.

    Note: we used to also fire on mid-sentence capitalized words,
    but EVERY English question contains a capitalized noun (Alice,
    Where, How) — that rule fired too often and pushed factoid
    queries ("Where does Alice work?") to verbatim-only when they
    should hit BOTH tiers. Now we require a quoted string OR an
    identifier (PR #1234, CVE-2024-..., v1.2.3, etc.) — both are
    strong signals the user wants exact-match recall.
    """
    if QUOTED_STRING.search(query):
        return True
    if IDENTIFIER.search(query):
        return True
    return False


# ---------------------------- router --------------------------------

def route(query: str) -> list[str]:
    """Decide which tier(s) to query.

    Returns a list of tier names:
      ['verbatim']    — factoid / exact-match
      ['structured']  — temporal / multi-hop
      ['verbatim', 'structured'] — ambiguous (run both, fuse)

    Order matters: the fusion bridge assigns slightly higher
    weight to the FIRST tier in the list (the router's "preferred"
    tier for this query).
    """
    if not query or not query.strip():
        return ["verbatim", "structured"]  # can't route nothing — try both

    # Rule 1: temporal
    if _has_temporal_signal(query):
        return ["structured"]

    # Rule 2: multi-hop
    if _has_multihop_signal(query):
        return ["structured"]

    # Rule 3: exact-phrase clue
    if _has_exact_phrase_signal(query):
        return ["verbatim"]

    # Rule 4: default — ambiguous, run both, let fusion decide
    return ["verbatim", "structured"]


# ---------------------------- explanation ---------------------------

def explain(query: str) -> dict:
    """Return a human-readable explanation of the routing decision.

    Useful for the trajectory viewer + audit log — shows the user
    WHY the system chose a particular tier for their query.
    """
    if not query or not query.strip():
        return {"tiers": ["verbatim", "structured"], "reason": "empty query"}

    signals = []
    if _has_temporal_signal(query):
        signals.append("temporal")
    if _has_multihop_signal(query):
        signals.append("multihop")
    if _has_exact_phrase_signal(query):
        signals.append("exact_phrase")

    if "temporal" in signals:
        return {"tiers": ["structured"],
                "reason": "temporal keywords detected — structured tier only"}
    if "multihop" in signals:
        return {"tiers": ["structured"],
                "reason": "multi-hop relation pattern detected — "
                          "structured tier only"}
    if "exact_phrase" in signals:
        return {"tiers": ["verbatim"],
                "reason": "exact-phrase clue (quoted string, "
                          "mid-sentence capitalized word, or identifier) "
                          "detected — verbatim tier only"}
    return {"tiers": ["verbatim", "structured"],
            "reason": "ambiguous query — running both tiers, "
                      "fusion bridge will rerank"}


__all__ = ["route", "explain"]
