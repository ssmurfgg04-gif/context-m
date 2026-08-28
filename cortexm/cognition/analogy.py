"""AnalogyDetector — finds structurally isomorphic domains via bipartite
relation mapping.

Fourth stage of the HMS cognition engine. Given two domains (sets of
entities + relations), find an isomorphism between their relation
graphs that maximizes structural overlap.

Classic example from Hofstadter/Mitchell's Copycat:
  "The atom is to the solar system as the cell is to the city."
  The detector finds that the (nucleus, orbits, electron) relation
  triple in atoms has the same SHAPE as (sun, orbits, planet) in solar
  systems and (mayor, governs, citizen) in cities.

For Context-M, we implement a simpler version: for each pair of
relations (r_a, r_b) with the same fanout pattern across subjects,
report an analogy. E.g.:
  (father, male_parent) and (mother, female_parent) have identical
  fanout → report analogy: father ≈ mother in shape (under sex-swap).

This is the seed for cross-domain transfer learning — once we know
the structural mapping, retrieval can find facts in domain A that
have analogues in domain B.

Hypotheses are emitted as derived facts:
  (r_a, ANALOGOUS_TO, r_b) with confidence < 0.5

So a reader can traverse the analogy graph to find structural
equivalents across domains.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from cortexm.cognition.scanner import ScanResult
from cortexm.trace.store import TraceStore
from cortexm.util import iso, new_id


# Edge kind used for analogy relations
ANALOGOUS_TO = "ANALOGOUS_TO"


@dataclass
class Analogy:
    """A structural analogy between two relations."""
    relation_a: str
    relation_b: str
    shared_fanout: int = 0   # how many subjects share the fanout pattern
    overlap_score: float = 0.0   # jaccard similarity of subject sets
    confidence: float = 0.0


@dataclass
class AnalogyResult:
    analogies: list[Analogy] = field(default_factory=list)
    edges_added: int = 0
    duration_ms: float = 0.0


class AnalogyDetector:
    """Finds structurally isomorphic relation pairs."""

    def __init__(self, store: TraceStore,
                 min_overlap: float = 0.40,
                 min_support: int = 2) -> None:
        self.store = store
        self.min_overlap = min_overlap
        self.min_support = min_support

    def run(self, scan: ScanResult, *,
            dry_run: bool = False,
            commit_id: str | None = None,
            user_id: str | None = None) -> AnalogyResult:
        """Find analogies by comparing relation subject sets."""
        import time
        t0 = time.perf_counter()

        # build a per-relation subject set
        rel_subjects: dict[str, set[str]] = defaultdict(set)
        where = "is_active=1 AND quarantined=0"
        args: tuple = ()
        if user_id is not None:
            where += " AND user_id=?"
            args = (user_id,)
        rows = self.store.conn.execute(
            f"SELECT relation, subject FROM facts WHERE {where}", args)
        for r in rows:
            rel_subjects[r[0]].add(r[1])

        # for each pair of relations with sufficient support, compute
        # the jaccard similarity of their subject sets
        relations = list(rel_subjects.keys())
        analogies: list[Analogy] = []
        for i, ra in enumerate(relations):
            sa = rel_subjects[ra]
            if len(sa) < self.min_support:
                continue
            for rb in relations[i + 1:]:
                sb = rel_subjects[rb]
                if len(sb) < self.min_support:
                    continue
                inter = len(sa & sb)
                if inter < self.min_support:
                    continue
                union = len(sa | sb)
                if union == 0:
                    continue
                jac = inter / union
                if jac < self.min_overlap:
                    continue
                analogies.append(Analogy(
                    relation_a=ra,
                    relation_b=rb,
                    shared_fanout=inter,
                    overlap_score=jac,
                    confidence=min(0.49, 0.10 + 0.20 * jac),
                ))

        # emit edges as derived facts: (ra, ANALOGOUS_TO, rb)
        edges_added = 0
        if not dry_run and analogies:
            ts = iso(_now())
            for a in analogies:
                fid = new_id()
                self.store.conn.execute(
                    "INSERT INTO facts "
                    "(id, subject, relation, value, valid_from, tx_from, "
                    " confidence, user_id, memory_type, is_derived, "
                    " is_active, birth_commit, provenance) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (fid, a.relation_a, ANALOGOUS_TO, a.relation_b,
                     ts, ts, a.confidence,
                     user_id or "default", "long_term",
                     1, 1, commit_id,
                     json.dumps({
                         "kind": "analogy",
                         "overlap_score": round(a.overlap_score, 4),
                         "shared_fanout": a.shared_fanout,
                         "generated_by": "cognition.analogy",
                     })))
                edges_added += 1
            if commit_id:
                self.store.update_commit_n_facts(commit_id, edges_added)

        return AnalogyResult(
            analogies=analogies,
            edges_added=edges_added,
            duration_ms=(time.perf_counter() - t0) * 1000.0)


def _now():
    return datetime.now(timezone.utc)


__all__ = ["AnalogyDetector", "Analogy", "AnalogyResult", "ANALOGOUS_TO"]
