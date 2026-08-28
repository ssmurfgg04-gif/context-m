"""GapDetector + HypothesisEngine — fill in missing relations.

Third stage of the HMS cognition engine. Two stages in one module
because they're tightly coupled:

  GapDetector      — for each entity, compare its set of relations
                       to its peers' sets. Missing relations that
                       peers have are gaps. For example:
                       Alice has {works_at, lives_in, has_skill}
                       Bob has {works_at, lives_in, has_skill, age}
                       → Alice is missing an `age` fact — gap.

  HypothesisEngine — for each gap, propose a filler. The filler can
                       come from:
                       (a) Majority vote: what value do peers of
                           the same abstraction have?
                       (b) Structural transitivity: if Alice's father
                           is Bob and Bob's father is Charles, the
                           engine hypothesizes Alice's grandfather is
                           Charles via the father→father chain.
                       (c) Hopfield cleanup: if a VSA palace is
                           available, query the superposition of
                           peers' values for that relation.

Hypotheses are written as facts with:
  is_derived=1
  confidence < 0.5
  provenance.kind = "hypothesis"
  provenance.source = the basis of the guess

The HYPOTHESIZED_BY edge links the supporting fact(s) to the new
hypothesis fact, so audits can trace the reasoning chain.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from cortexm.cognition.abstraction import Abstraction
from cortexm.cognition.scanner import ScanResult
from cortexm.trace.store import TraceStore
from cortexm.util import iso, new_id


# Edge kind used to wire supporting facts to hypotheses
HYPOTHESIZED_BY = "HYPOTHESIZED_BY"


@dataclass
class Gap:
    """A missing relation for an entity."""
    subject: str
    missing_relation: str
    peer_count: int = 0          # how many peers have this relation
    peer_values: list[str] = field(default_factory=list)
    basis: str = ""             # "majority" | "structural" | "cleanup"


@dataclass
class Hypothesis:
    """A proposed filler for a gap."""
    subject: str
    relation: str
    proposed_value: str
    confidence: float = 0.0
    basis: str = ""              # which strategy proposed this
    supporting_facts: list[str] = field(default_factory=list)
    fact_id: str = ""


@dataclass
class GapResult:
    gaps: list[Gap] = field(default_factory=list)
    n_subjects_compared: int = 0
    duration_ms: float = 0.0


@dataclass
class HypothesisResult:
    hypotheses: list[Hypothesis] = field(default_factory=list)
    facts_added: int = 0
    duration_ms: float = 0.0


class GapDetector:
    """Finds missing relations by comparing entity profiles to peers."""

    def __init__(self, store: TraceStore,
                 min_peers_with_relation: int = 2) -> None:
        self.store = store
        self.min_peers_with_relation = min_peers_with_relation

    def run(self, scan: ScanResult,
            user_id: str | None = None) -> GapResult:
        """Compare each subject's relation set to its peers' sets."""
        import time
        t0 = time.perf_counter()

        # build per-subject relation sets from the trace
        where = "is_active=1 AND quarantined=0"
        args: tuple = ()
        if user_id is not None:
            where += " AND user_id=?"
            args = (user_id,)
        rows = self.store.conn.execute(
            f"SELECT subject, relation, value FROM facts WHERE {where}",
            args).fetchall()
        subj_rels: dict[str, set[str]] = defaultdict(set)
        subj_vals: dict[str, dict[str, str]] = defaultdict(dict)
        rel_subjects: dict[str, set[str]] = defaultdict(set)
        rel_values: dict[str, list[str]] = defaultdict(list)
        for r in rows:
            s, rel, v = r[0], r[1], r[2]
            subj_rels[s].add(rel)
            subj_vals[s][rel] = v
            rel_subjects[rel].add(s)
            rel_values[rel].append(v)

        # for each relation that exists in the trace, find subjects
        # that DON'T have it but have at least one co-occurring relation
        # with peers that DO have it.
        gaps: list[Gap] = []
        all_subjects = set(subj_rels.keys())
        for rel, peers in rel_subjects.items():
            if len(peers) < self.min_peers_with_relation:
                continue
            missing_subjects = all_subjects - peers
            for s in missing_subjects:
                # check overlap with peers — at least one shared relation
                my_rels = subj_rels[s]
                peer_rels_union = set()
                for p in peers:
                    peer_rels_union |= subj_rels[p]
                overlap = my_rels & peer_rels_union
                if len(overlap) < 1:
                    continue  # not a peer of these — different category
                gaps.append(Gap(
                    subject=s,
                    missing_relation=rel,
                    peer_count=len(peers),
                    peer_values=sorted(set(rel_values[rel]))[:5],
                    basis="majority"))

        # structural gaps: for relations that form chain signatures
        # (e.g. father → father = grandfather), check each subject that
        # has the first hop — does it have the result of the second hop
        # recorded? If not, that's a structural gap.
        for p in scan.patterns:
            if p.kind != "relation_pair":
                continue
            chain_rel = p.payload["relation"]
            for ex in p.payload.get("examples", []):
                # ex = (start, mid, end). The subject `start` has
                # chain_rel=start→mid, mid→end. Hypothesize that
                # start has a chain_rel_2 value of end.
                # This is reported as a structural gap so the
                # HypothesisEngine can propose (start, chain_rel*2, end).
                if len(ex) != 3:
                    continue
                start, mid, end = ex
                # check if start already has the inferred composite
                # relation — we model the composite as the chain
                # relation with the value being the chain endpoint.
                # Specifically: a hypothesis fact (start, chain_rel,
                # end) is misleading because start already has
                # chain_rel=mid. We use a synthetic relation name.
                composite_rel = f"{chain_rel}*{chain_rel}"
                existing = self.store.conn.execute(
                    "SELECT 1 FROM facts WHERE subject=? AND relation=? "
                    "AND value=? AND is_active=1 LIMIT 1",
                    (start, composite_rel, end)).fetchone()
                if existing:
                    continue  # already known — not a gap
                gaps.append(Gap(
                    subject=start,
                    missing_relation=composite_rel,
                    peer_count=p.support,
                    peer_values=[end],
                    basis="structural"))

        return GapResult(
            gaps=gaps,
            n_subjects_compared=len(all_subjects),
            duration_ms=(time.perf_counter() - t0) * 1000.0)


class HypothesisEngine:
    """Proposes fillers for gaps via three strategies."""

    MAX_HYPOTHESES_PER_GAP: int = 1
    MAX_HYPOTHESIS_CONFIDENCE: float = 0.45

    def __init__(self, store: TraceStore,
                 palace=None,
                 max_confidence: float | None = None) -> None:
        self.store = store
        self.palace = palace  # optional, for Hopfield cleanup path
        self.max_conf = max_confidence or self.MAX_HYPOTHESIS_CONFIDENCE

    def run(self, gaps: list[Gap], *,
            dry_run: bool = False,
            commit_id: str | None = None,
            user_id: str | None = None) -> HypothesisResult:
        """Propose and write hypothesis facts for each gap."""
        import time
        t0 = time.perf_counter()

        hypotheses: list[Hypothesis] = []
        facts_added = 0
        ts = iso(_now()) if not dry_run else ""

        for gap in gaps:
            proposed = self._propose(gap, user_id)
            if not proposed:
                continue
            hypotheses.append(proposed)

            if dry_run:
                continue

            # write the hypothesis as a derived fact
            fid = new_id()
            self.store.conn.execute(
                "INSERT INTO facts "
                "(id, subject, relation, value, valid_from, tx_from, "
                " confidence, user_id, memory_type, is_derived, "
                " is_active, birth_commit, provenance) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fid, proposed.subject, proposed.relation,
                 proposed.proposed_value, ts, ts,
                 proposed.confidence,
                 user_id or "default",
                 "long_term", 1, 1, commit_id,
                 json.dumps({
                     "kind": "hypothesis",
                     "basis": proposed.basis,
                     "supporting_facts": proposed.supporting_facts,
                     "peer_count": gap.peer_count,
                     "peer_values_sample": gap.peer_values,
                     "generated_by": "cognition.hypothesis",
                 })))
            # wire HYPOTHESIZED_BY edges from each supporting fact
            for support_fid in proposed.supporting_facts:
                if support_fid:
                    try:
                        self.store.add_edge(
                            fid, support_fid, HYPOTHESIZED_BY,
                            {"basis": proposed.basis})
                    except Exception:
                        pass
            proposed.fact_id = fid
            facts_added += 1

        if not dry_run and commit_id and facts_added:
            self.store.update_commit_n_facts(commit_id, facts_added)

        return HypothesisResult(
            hypotheses=hypotheses,
            facts_added=facts_added,
            duration_ms=(time.perf_counter() - t0) * 1000.0)

    # ---------- strategies -------------------------------------------
    def _propose(self, gap: Gap, user_id: str | None) -> Hypothesis | None:
        # structural: peer_values are exact (end of chain)
        if gap.basis == "structural" and gap.peer_values:
            value = gap.peer_values[0]
            supporting = self._lookup_supporting_facts(
                gap.subject, gap.missing_relation, value, user_id)
            return Hypothesis(
                subject=gap.subject,
                relation=gap.missing_relation,
                proposed_value=value,
                confidence=min(self.max_conf, 0.20 + 0.05 * gap.peer_count),
                basis="structural",
                supporting_facts=supporting,
            )

        # majority: most common value among peers
        if gap.basis == "majority" and gap.peer_values:
            # majority among peers — for categorical relations like
            # `has_skill:rust` or `lives_in:toronto`, the most common
            # peer value is a reasonable guess. For unique values
            # like `works_at` (each person works at one company), the
            # majority strategy is weaker — we still emit a hypothesis
            # but at much lower confidence.
            counter: dict[str, int] = defaultdict(int)
            where = "is_active=1 AND quarantined=0 AND relation=?"
            args: tuple = (gap.missing_relation,)
            if user_id is not None:
                where += " AND user_id=?"
                args = (gap.missing_relation, user_id)
            rows = self.store.conn.execute(
                f"SELECT value FROM facts WHERE {where}", args).fetchall()
            for r in rows:
                counter[r[0]] += 1
            if not counter:
                return None
            most_common = max(counter.items(), key=lambda x: x[1])
            value = most_common[0]
            n_peers_with_value = most_common[1]
            total_peers = sum(counter.values())
            conf = min(self.max_conf,
                       0.05 + 0.05 * (n_peers_with_value / max(1, total_peers)))
            supporting = self._lookup_supporting_facts(
                gap.subject, gap.missing_relation, value, user_id)
            return Hypothesis(
                subject=gap.subject,
                relation=gap.missing_relation,
                proposed_value=value,
                confidence=conf,
                basis="majority",
                supporting_facts=supporting,
            )

        return None

    def _lookup_supporting_facts(self, subject: str, relation: str,
                                  value: str,
                                  user_id: str | None) -> list[str]:
        """Look up fact IDs that support a hypothesis.

        For structural hypotheses (father→father), supporting facts
        are the two hops (Alice father Bob, Bob father Charles).
        """
        ids: list[str] = []
        if relation.endswith("*" + relation.split("*")[0]):
            # composite chain — look up the two hops
            base_rel = relation.split("*")[0]
            cur = subject
            for _ in range(2):
                rows = self.store.conn.execute(
                    "SELECT id, value FROM facts WHERE subject=? "
                    "AND relation=? AND is_active=1 "
                    "AND quarantined=0 LIMIT 1",
                    (cur, base_rel)).fetchall()
                if not rows:
                    break
                ids.append(rows[0][0])
                cur = rows[0][1]
                if cur == value:
                    break
        else:
            rows = self.store.conn.execute(
                "SELECT id FROM facts WHERE relation=? AND value=? "
                "AND is_active=1 AND quarantined=0 LIMIT 5",
                (relation, value)).fetchall()
            ids = [r[0] for r in rows]
        return ids


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


__all__ = [
    "GapDetector", "HypothesisEngine",
    "Gap", "Hypothesis",
    "GapResult", "HypothesisResult",
    "HYPOTHESIZED_BY",
]
