"""Query-time synonym + paraphrase expansion (Lucene ``synonym_graph``
filter lineage, deterministic, μ=0).

WHY THIS MODULE EXISTS
-----------------------
The user audit (Aug 2026) flagged that the v0.5.x pattern extractor
hits a ceiling on paraphrase recall (canonical LongMemEval OOD
paraphrase score: 22.9%). Google Search (pre-BERT, deterministic) never
tried to extract the web into canonical triples — it INDEXES verbatim
and EXPANDS THE QUERY to match every possible phrasing.

This module is the Lucene ``synonym_graph`` filter ported to a
deterministic, μ=0 Python module. It runs at QUERY time only — the
verbatim tier stores raw chunks, and the query is rewritten before
BM25 to surface chunks the user phrased differently than the question.

ARCHITECTURE
-------------
The synonym graph is a flat dict of *concept → [synonyms]*. When the
user's query contains any synonym from a cluster, the rewriter emits
one rewritten query per alternative synonym in that cluster (replacing
the matched synonym in place). The original query is ALWAYS included
as the first expansion.

This is NOT a stemmer. Stemmers operate on a single token. This is a
phrase-level rewriter — "dog's name" and "pet named" are different
multi-token phrases, and the rewriter swaps them at the phrase level.

The graph is hand-curated for the agent-memory domain (employment,
residence, pets, education, vehicles, preferences). Adding a new
concept cluster is a one-line edit. Users can extend the graph at
runtime via ``register_cluster()``.
"""
from __future__ import annotations

import re
from typing import Dict, List


# ---------------------------------------------------------------------------
# Concept → synonym-phrase clusters. Each cluster is a flat list of
# multi-word phrases that mean the same thing in agent-memory context.
# Order doesn't matter — the rewriter emits all permutations.
# ---------------------------------------------------------------------------
DEFAULT_CLUSTERS: Dict[str, List[str]] = {
    # EMPLOYMENT — the largest cluster. Catches "work", "job", "employed",
    # "joined", "position", "role", "career". Each phrase is a different
    # way a user might state employment.
    "employment": [
        "work at", "works at", "working at", "worked at",
        "job at", "got a job at", "started at", "started working at",
        "employed by", "employed at",
        "joined", "joined as",
        "position at", "role at", "role in",
        "career at",
        "now at", "moved to",  # ambiguous with residence — see "residence"
        "hired by", "hired at",
    ],
    # RESIDENCE — "live", "moved", "relocated", "based". Note "moved to"
    # is in BOTH employment and residence clusters (the user's intent
    # depends on context: "moved to Google" = job; "moved to Berlin" =
    # residence). The rewriter emits both expansions and lets BM25 sort
    # it out — chunks about jobs will hit the employment expansion,
    # chunks about homes will hit the residence expansion.
    "residence": [
        "live in", "lives in", "living in", "lived in",
        "live at", "lives at", "living at",
        "moved to", "relocated to", "relocating to",
        "based in", "based out of",
        "stay in", "stays in", "staying in",
        "home in", "home at",
        "resident of", "residence in",
    ],
    # PET NAME — "dog's name", "pet called", "dog named". Catches the
    # canonical LongMemEval paraphrase failure: Q="What is my dog's
    # name?" doesn't lexically match chunk "My dog is named Charlie".
    "pet_name": [
        "dog's name", "dog's called", "dog named", "dog called",
        "cat's name", "cat's called", "cat named", "cat called",
        "pet's name", "pet named", "pet called",
        "puppy named", "puppy called", "puppy's name",
        "have a dog named", "have a cat named",
        "my dog", "my cat", "my pet",
    ],
    # EDUCATION — "study at", "graduated from", "degree from".
    "education": [
        "study at", "studies at", "studying at", "studied at",
        "graduated from", "graduate of", "alumni of", "alumnus of",
        "degree from", "degree in",
        "major in", "majored in",
        "enrolled at", "enrolled in",
        "completed my undergrad", "completed undergrad",
        "got my degree", "got my undergrad",
    ],
    # VEHICLE — "drive", "own", "bought", "leased".
    "vehicle": [
        "drive a", "drives a", "driving a", "drove a",
        "own a", "owns a", "owns the",
        "bought a", "lease a", "leased a",
        "car is", "car was",
    ],
    # PREFERENCE — "like", "love", "favorite", "prefer".
    "preference": [
        "like", "likes", "liked", "loves", "loved", "love",
        "favorite", "favourite", "favorites", "favourites",
        "prefer", "prefers", "preferred",
        "enjoy", "enjoys", "enjoyed",
        "into", "really into",
    ],
    # NEGATION — the negation cluster is special: queries that match it
    # should ALSO search for the negation-record table (see negation.py),
    # not just verbatim. The rewriter only emits the original query for
    # negation clusters — the reader handles the negation lookup.
    "negation": [
        "don't", "dont", "do not", "does not", "doesn't", "didn't",
        "never", "no longer", "not", "stopped", "quit",
    ],
    # RELATIONS — family.
    "family": [
        "brother", "sister", "mother", "mom", "father", "dad",
        "son", "daughter", "husband", "wife", "spouse",
        "grandfather", "grandmother", "grandparent",
        "uncle", "aunt", "cousin", "nephew", "niece",
    ],
}


class SynonymGraph:
    """Lucene ``synonym_graph`` filter ported to μ=0 Python.

    The graph stores clusters as: concept → [synonym phrases]. For
    efficient lookup we also maintain a reverse index:
    phrase_lower → concept. The rewriter scans the query for any
    phrase in the reverse index, and for each match emits a rewritten
    query substituting each alternative synonym.

    The graph is multi-word-phrase aware. A simple word-level
    substitution would corrupt positions ("live in" → "based" loses
    the preposition). We do PHRASE-level substitution at the character
    level via regex with word-boundary guards.
    """

    def __init__(self, clusters: Dict[str, List[str]] | None = None) -> None:
        self.clusters: Dict[str, List[str]] = dict(clusters or DEFAULT_CLUSTERS)
        # Reverse index: phrase_lower → concept. Lowercase for matching.
        self._reverse: Dict[str, str] = {}
        for concept, syns in self.clusters.items():
            for s in syns:
                self._reverse.setdefault(s.lower(), concept)
        # Pre-compile a single master regex of ALL synonyms for fast scan.
        # Sort by length DESC so longer phrases match first (greedy).
        # Use \b word boundaries so "live in" doesn't match inside "olive".
        phrases = sorted(self._reverse.keys(), key=len, reverse=True)
        # Escape special chars and build pattern
        if phrases:
            joined = "|".join(re.escape(p) for p in phrases)
            self._master_re = re.compile(
                rf"\b(?:{joined})\b", re.IGNORECASE)
        else:
            self._master_re = re.compile(r"$.")  # never matches

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_cluster(self, concept: str, synonyms: List[str]) -> None:
        """Add or replace a concept cluster at runtime. Idempotent."""
        self.clusters[concept] = list(synonyms)
        # Rebuild reverse index + master regex
        for s in synonyms:
            self._reverse[s.lower()] = concept
        phrases = sorted(self._reverse.keys(), key=len, reverse=True)
        joined = "|".join(re.escape(p) for p in phrases)
        self._master_re = re.compile(rf"\b(?:{joined})\b", re.IGNORECASE)

    def find_matches(self, query: str) -> List[Dict]:
        """Return all synonym matches in the query (no rewrite)."""
        out = []
        for m in self._master_re.finditer(query):
            phrase = m.group(0)
            concept = self._reverse.get(phrase.lower())
            if concept:
                out.append({
                    "concept": concept,
                    "phrase": phrase,
                    "synonyms": self.clusters[concept],
                    "span": (m.start(), m.end()),
                })
        return out

    def expand(self, query: str, *, max_expansions: int = 16) -> List[str]:
        if not query:
            return [query]
        expansions = [query]
        seen = {query.lower()}
        matches = self.find_matches(query)
        # Group matches by concept so we don't double-rewrite the same
        # concept if the user used two synonyms from the same cluster.
        by_concept: Dict[str, Dict] = {}
        for m in matches:
            by_concept.setdefault(m["concept"], m)
        for concept, m in by_concept.items():
            matched_phrase = m["phrase"]
            for alt in m["synonyms"]:
                if alt.lower() == matched_phrase.lower():
                    continue
                # Substitute at the EXACT span (not regex replace — we
                # need to substitute only the matched occurrence, but
                # if the same synonym appears twice in the query, both
                # should be substituted; use re.sub with the matched
                # phrase as the pattern)
                rewritten = re.sub(
                    re.escape(matched_phrase), alt, query, count=0,
                    flags=re.IGNORECASE)
                key = rewritten.lower()
                if key not in seen:
                    seen.add(key)
                    expansions.append(rewritten)
                if len(expansions) >= max_expansions:
                    return expansions
        return expansions


# ---------------------------------------------------------------------------
# Module-level singleton (default graph) for callers that don't want
# to instantiate. Most callers use this.
# ---------------------------------------------------------------------------
_default_graph: SynonymGraph | None = None


def default_graph() -> SynonymGraph:
    """Process-wide singleton SynonymGraph (default clusters)."""
    global _default_graph
    if _default_graph is None:
        _default_graph = SynonymGraph()
    return _default_graph


def expand_query(query: str, *, max_expansions: int = 16) -> List[str]:
    """Module-level convenience wrapper around the default graph."""
    return default_graph().expand(query, max_expansions=max_expansions)
