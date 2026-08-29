"""Query-time orchestrator: FST + synonyms + recognizers + slang.

WHY THIS MODULE EXISTS
-----------------------
The user audit (Aug 2026) asked for "query-time expansion, not more
extraction patterns." The Google pre-BERT strategy is:
  1. Normalize the query (spelling + abbreviation expansion)
  2. Apply slang normalization
  3. Expand synonyms (Lucene synonym_graph filter)
  4. Resolve entities (holidays, currency, dates) → emit alternative
     queries with resolved forms
  5. Run BM25 on EACH expansion and union the results

This module is the orchestrator that wires those four stages together
in the right order. Each stage is a separate module so it's testable
in isolation; this module just sequences them.

ARCHITECTURE
-------------
The orchestrator is a single class ``QueryRewriter`` with one public
method ``rewrite(query)`` returning ``List[str]`` (the list of
expanded queries, original first).

The caller (VerbatimPlugin.search or Memory.search) takes the list
and runs BM25 on each, deduping by rowid.

Order of stages (matters):
  1. SLANG — apply first so "bruh I deadass work at Google no cap"
     normalizes to "I seriously work at Google truthfully" before
     anything else. Slang removal simplifies the query for downstream
     stages.
  2. FST (spelling + abbreviation) — apply on the normalized text.
     "Recieve money from UCLA" → "Receive money from University of
     California Los Angeles".
  3. SYNONYMS — apply on the FST output. "Where do I work?" matches
     the "employment" cluster → emit expansions with "job at",
     "employed by", etc.
  4. RECOGNIZERS — extract holiday names + currency + dates and
     emit additional queries where the holiday name is replaced by
     its resolved ISO date. "I volunteered on Valentine's Day" →
     "I volunteered on 2026-02-14". This catches the canonical
     LongMemEval holiday ground-truth pattern (user says holiday
     name; expected answer is the date).

μ=0: every stage is deterministic. No LLM, no API.
"""
from __future__ import annotations

import re
from typing import List

from cortexm.bridge.fst import QueryFST, default_fst
from cortexm.bridge.recognizers import (
    DeterministicRecognizer, default_recognizer,
)
from cortexm.bridge.slang import SlangNormalizer, default_normalizer as slang_default
from cortexm.bridge.synonyms import SynonymGraph, default_graph
from cortexm.bridge.negation import detect_negation


class QueryRewriter:
    """Query-time orchestrator.

    Constructor takes optional overrides for each stage so callers
    can plug in custom dictionaries (e.g. a domain-specific synonym
    cluster for medical or legal text).
    """

    def __init__(self,
                 slang: SlangNormalizer | None = None,
                 fst: QueryFST | None = None,
                 synonyms: SynonymGraph | None = None,
                 recognizer: DeterministicRecognizer | None = None,
                 *,
                 max_expansions: int = 16) -> None:
        self.slang = slang or slang_default()
        self.fst = fst or default_fst()
        self.synonyms = synonyms or default_graph()
        self.recognizer = recognizer or default_recognizer()
        self.max_expansions = max_expansions

    def rewrite(self, query: str, *, year: int | None = None) -> List[str]:
        """Return the list of expanded queries (original first).

        Each expansion is a fully-formed natural-language query string
        ready for FTS5 BM25 search.

        The list is deduplicated (case-insensitive). Capped at
        ``max_expansions`` to keep the BM25 fan-out bounded.

        The original query is ALWAYS first (so callers can fall back
        to it if all expansions fail).
        """
        if not query:
            return [query]
        # Stage 1: slang normalization (applied to the original query)
        stage1 = self.slang.normalize(query)
        # Stage 2: FST normalization (abbreviation + spelling)
        stage2 = self.fst.normalize(stage1)
        # Stage 3: synonym expansion (synonym_graph filter)
        stage3 = self.synonyms.expand(stage2, max_expansions=self.max_expansions)
        # Stage 3 always includes the original (post-FST) query as its
        # first element. Stage 4 runs on EACH of those expansions.
        # Stage 4: entity resolution — for each expansion, if it
        # contains a holiday name, emit an additional expansion where
        # the holiday is replaced by its resolved date.
        from cortexm.bridge.recognizers import _STATIC_HOLIDAYS, _ALGORITHMIC_HOLIDAYS
        # Sort holidays LONGEST FIRST so "valentine's day" matches before
        # "valentine" — this avoids the partial-match bug where
        # "valentine" inside "valentine's day" gets replaced, producing
        # "2026-02-14's day". The outer search() also requires the char
        # AFTER the match to NOT be a letter or apostrophe (so "valentine"
        # inside "valentine's" is skipped — the longer holiday name
        # "valentine's day" gets matched instead).
        all_holidays = sorted(
            list(_STATIC_HOLIDAYS.keys()) + list(_ALGORITHMIC_HOLIDAYS.keys()),
            key=len, reverse=True)
        final: List[str] = []
        seen = set()
        for q in stage3:
            ql = q.lower()
            if ql not in seen:
                seen.add(ql)
                final.append(q)
            # Track spans already replaced (so "valentine" doesn't fire
            # inside a "valentine's day" match we already emitted).
            consumed_spans: list[tuple[int, int]] = []
            for h in all_holidays:
                # Lookahead: the next char after the match must NOT be a
                # letter, apostrophe, or hyphen (otherwise we're matching
                # a sub-phrase). Use negative-lookahead in the regex.
                pat = re.compile(
                    rf"\b{re.escape(h)}\b(?![\w'\-])", re.IGNORECASE)  # noqa: E501
                for m in pat.finditer(q):
                    s, e = m.start(), m.end()
                    # Skip if this span overlaps an already-consumed span
                    if any(s < ce and cs < e for cs, ce in consumed_spans):
                        continue
                    resolved = self.recognizer.resolve_holiday(
                        h, year=year)
                    if resolved:
                        rewritten = q[:s] + resolved + q[e:]
                        rl = rewritten.lower()
                        if rl not in seen:
                            seen.add(rl)
                            final.append(rewritten)
                            consumed_spans.append((s, e + len(resolved) - (e - s)))
                        if len(final) >= self.max_expansions:
                            return final
            if len(final) >= self.max_expansions:
                return final
        return final[:self.max_expansions]

    def detect_negation(self, query: str) -> bool:
        """True if the query itself contains a negation marker.

        Used by the reader to decide whether to also query the
        negation_records table.
        """
        return bool(detect_negation(query))


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_default: QueryRewriter | None = None


def default_rewriter() -> QueryRewriter:
    global _default
    if _default is None:
        _default = QueryRewriter()
    return _default


def rewrite(query: str, *, year: int | None = None) -> List[str]:
    """Module-level convenience wrapper."""
    return default_rewriter().rewrite(query, year=year)
