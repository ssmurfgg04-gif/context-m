"""PatternScanner — surfaces structural regularities across triples.

The first stage of the HMS cognition engine. Scans the Trace for
recurring patterns in the (subject, relation, value) graph and reports:

  - Relation frequency histogram (which relations are common)
  - Subject fan-out (entities with many relations — hubs)
  - Value fan-out (values that appear for many subjects — categories)
  - Co-occurring relations (e.g. works_at + lives_in co-occur on
    80% of subjects → suggest a 'person' abstraction)
  - Relation pair signatures (e.g. father→father appears 12 times
    → suggest a 'grandfather' composite relation)

Output is a list of `Pattern` records stored in the engine's working
state. The next stage (AbstractionEngine) consumes these patterns to
build prototype categories.

This module is read-only against the Trace — it never writes. The
HypothesisEngine later writes HYPOTHESIZED_BY edges; the scanner only
OBSERVES.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from context_m.trace.store import TraceStore


@dataclass
class Pattern:
    """A structural regularity the scanner surfaced."""
    kind: str                # "relation_freq" | "subject_fanout" |
                            # "value_fanout" | "co_occur" | "relation_pair"
    payload: dict            # kind-specific fields
    support: int             # how many facts support this pattern
    confidence: float = 0.0  # 0..1 — strength of the regularity


@dataclass
class ScanResult:
    """Result of a single PatternScanner.run() pass."""
    patterns: list[Pattern] = field(default_factory=list)
    n_facts_scanned: int = 0
    n_relations: int = 0
    n_subjects: int = 0
    n_values: int = 0
    duration_ms: float = 0.0


class PatternScanner:
    """Surfaces structural regularities across the Trace."""

    # minimum support for a pattern to be considered significant
    MIN_SUPPORT: int = 2
    # minimum co-occurrence fraction (0..1) for a co-occur pattern
    CO_OCCUR_MIN: float = 0.30

    def __init__(self, store: TraceStore,
                 min_support: int | None = None,
                 co_occur_min: float | None = None) -> None:
        self.store = store
        self.min_support = min_support or self.MIN_SUPPORT
        self.co_occur_min = co_occur_min or self.CO_OCCUR_MIN

    def run(self, user_id: str | None = None) -> ScanResult:
        """Scan the Trace and return structural patterns."""
        import time
        t0 = time.perf_counter()
        where = "is_active=1 AND quarantined=0"
        args: tuple = ()
        if user_id is not None:
            where += " AND user_id=?"
            args = (user_id,)
        rows = self.store.conn.execute(
            f"SELECT subject, relation, value FROM facts WHERE {where}",
            args).fetchall()

        rel_counter: Counter = Counter()
        subj_rels: defaultdict[str, set[str]] = defaultdict(set)
        subj_vals: defaultdict[str, set[str]] = defaultdict(set)
        val_subjs: defaultdict[str, set[str]] = defaultdict(set)
        pair_counter: Counter = Counter()
        pair_examples: dict[tuple[str, str], list[str]] = {}

        subjects: list[str] = []
        for r in rows:
            s, rel, v = r[0], r[1], r[2]
            rel_counter[rel] += 1
            subj_rels[s].add(rel)
            subj_vals[s].add(v)
            val_subjs[v].add(s)
            subjects.append(s)

        # relation pair signatures — for each subject, look at all
        # ordered pairs of relations it has, then for each pair count
        # how often a subject has both (this is the basis for the
        # "co_occur" pattern). We also count explicit pair chains like
        # (father, father) which signals a grandfather composite.
        seen_subj: set[str] = set()
        for s in subjects:
            if s in seen_subj:
                continue
            seen_subj.add(s)
            rels = list(subj_rels[s])
            for i, r1 in enumerate(rels):
                for r2 in rels[i + 1:]:
                    pair_counter[(r1, r2)] += 1
                    pair_counter[(r2, r1)] += 1
                    pair_examples.setdefault((r1, r2), []).append(s)

        # check for self-pair chains (r1 followed by r1 on a different
        # subject — e.g. father(A, B), father(B, C) means A's grandfather
        # is C). This requires walking the value-as-next-subject chain.
        # Implementation: for each (subj, rel, val), check if there's
        # another fact with subj=val, rel=rel. If so, we have a 2-hop
        # chain and (rel, rel) is a relation_pair pattern.
        chain_examples: dict[str, list[tuple[str, str, str]]] = {}
        chain_counter: Counter = Counter()
        for r in rows:
            s, rel, v = r[0], r[1], r[2]
            # does (v, rel, ?) exist?
            sub = self.store.conn.execute(
                "SELECT value FROM facts WHERE subject=? AND relation=? "
                "AND is_active=1 AND quarantined=0 LIMIT 5",
                (v, rel)).fetchall()
            for sr in sub:
                chain_counter[rel] += 1
                chain_examples.setdefault(rel, []).append(
                    (s, v, sr[0]))

        patterns: list[Pattern] = []

        # ---- 1. relation_freq ----
        for rel, cnt in rel_counter.most_common():
            if cnt >= self.min_support:
                patterns.append(Pattern(
                    kind="relation_freq",
                    payload={"relation": rel},
                    support=cnt,
                    confidence=min(1.0, cnt / max(1, len(rows)))))

        # ---- 2. subject_fanout ----
        subj_fanout = sorted(
            ((s, len(rels)) for s, rels in subj_rels.items()),
            key=lambda x: -x[1])
        for s, cnt in subj_fanout:
            if cnt >= max(2, self.min_support):
                patterns.append(Pattern(
                    kind="subject_fanout",
                    payload={"subject": s, "relations": sorted(subj_rels[s])},
                    support=cnt,
                    confidence=min(1.0, cnt / 10.0)))  # 10 rels = max

        # ---- 3. value_fanout ----
        val_fanout = sorted(
            ((v, len(subjs)) for v, subjs in val_subjs.items()),
            key=lambda x: -x[1])
        for v, cnt in val_fanout:
            if cnt >= max(2, self.min_support):
                patterns.append(Pattern(
                    kind="value_fanout",
                    payload={"value": v, "subjects": sorted(val_subjs[v])},
                    support=cnt,
                    confidence=min(1.0, cnt / 20.0)))

        # ---- 4. co_occur (relation pairs on same subject) ----
        n_subjects = len(seen_subj)
        for (r1, r2), cnt in pair_counter.most_common(20):
            if cnt < self.min_support:
                continue
            frac = cnt / max(1, n_subjects)
            if frac >= self.co_occur_min:
                patterns.append(Pattern(
                    kind="co_occur",
                    payload={"rel_a": r1, "rel_b": r2,
                             "examples": pair_examples.get((r1, r2), [])[:5]},
                    support=cnt,
                    confidence=frac))

        # ---- 5. relation_pair (chain signatures) ----
        for rel, cnt in chain_counter.most_common():
            if cnt >= self.min_support:
                ex = chain_examples.get(rel, [])[:5]
                patterns.append(Pattern(
                    kind="relation_pair",
                    payload={"relation": rel, "chain": [rel, rel],
                             "examples": ex},
                    support=cnt,
                    confidence=min(1.0, cnt / 10.0)))

        return ScanResult(
            patterns=patterns,
            n_facts_scanned=len(rows),
            n_relations=len(rel_counter),
            n_subjects=n_subjects,
            n_values=len(val_subjs),
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        )


__all__ = ["PatternScanner", "Pattern", "ScanResult"]
