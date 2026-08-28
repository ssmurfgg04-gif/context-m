"""AbstractionEngine — bundles atom vectors into prototype categories.

Second stage of the HMS cognition engine. Consumes the patterns
surfaced by PatternScanner and builds prototype categories:

  - When N entities share a relation pattern (e.g. works_at + lives_in),
    create a prototype "person" category and tag those entities as
    members.
  - When N values appear for the same relation across subjects
    (value_fanout), those values become "category centroids" — e.g.
    if (Google, Stripe, Anthropic) all appear as works_at values,
    they form a "tech_company" prototype.

The prototype is stored as a derived fact in the Trace:
  (proto:<name>, member_of, <entity>)
  with provenance.kind = "abstraction" and confidence < 0.5

This stage does NOT delete facts — it adds new derived facts that
link entities to abstractions. The derived facts are tagged
`is_derived=1` so they don't pollute the active fact count.

Prototype vectors are also computed in the VSA palace (superposition
of members' holograms) so retrieval can match against the prototype
directly — but this is the palace's responsibility, not the
abstraction engine's. We only emit the membership edges here.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from cortexm.cognition.scanner import Pattern, ScanResult
from cortexm.trace.store import TraceStore
from cortexm.util import iso, new_id


@dataclass
class Abstraction:
    """A prototype category discovered by AbstractionEngine."""
    name: str                # e.g. "person" or "tech_company"
    kind: str                 # "subject_role" | "value_cluster"
    members: list[str] = field(default_factory=list)
    prototype_relations: list[str] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class AbstractionResult:
    abstractions: list[Abstraction] = field(default_factory=list)
    membership_edges_added: int = 0
    duration_ms: float = 0.0


class AbstractionEngine:
    """Builds prototype categories from surfaced patterns."""

    def __init__(self, store: TraceStore,
                 min_members: int = 2,
                 min_co_occur_support: int = 2) -> None:
        self.store = store
        self.min_members = min_members
        self.min_co_occur_support = min_co_occur_support

    def run(self, scan: ScanResult, *,
            dry_run: bool = False,
            commit_id: str | None = None,
            user_id: str | None = None) -> AbstractionResult:
        """Build abstractions from scan results."""
        import time
        t0 = time.perf_counter()

        # subject-role abstractions: subjects with the same set of
        # relations (e.g. {works_at, lives_in, has_skill}) form a
        # "person" prototype. Group subjects by frozenset(relations).
        rel_groups: dict[frozenset, list[str]] = defaultdict(list)
        for p in scan.patterns:
            if p.kind != "subject_fanout":
                continue
            if p.support < self.min_co_occur_support:
                continue
            rels = frozenset(p.payload.get("relations", []))
            if len(rels) >= 2:
                rel_groups[rels].append(p.payload["subject"])

        # value-cluster abstractions: values that appear for the same
        # relation across N subjects form a "value_cluster" prototype
        # (e.g. tech_company for {Google, Stripe, Anthropic, OpenAI})
        val_groups: dict[tuple[str, list[str]], list[str]] = defaultdict(list)
        for p in scan.patterns:
            if p.kind != "value_fanout":
                continue
            if p.support < self.min_members:
                continue
            # which relation does this value cluster under? We don't
            # know directly from the pattern — look it up.
            val = p.payload["value"]
            subjs = p.payload["subjects"]
            # find the relation(s) that produce this value across subjects
            rel_rows = self.store.conn.execute(
                "SELECT DISTINCT relation FROM facts WHERE value=? "
                "AND is_active=1 AND quarantined=0 LIMIT 3",
                (val,)).fetchall()
            for r in rel_rows:
                rel = r[0]
                key = (rel, [val])
                val_groups[key].extend(subjs)

        abstractions: list[Abstraction] = []
        # synthesize subject-role abstractions
        proto_idx = 0
        for rels, members in rel_groups.items():
            if len(members) < self.min_members:
                continue
            name = f"proto:role_{proto_idx}"
            proto_idx += 1
            abstractions.append(Abstraction(
                name=name,
                kind="subject_role",
                members=members,
                prototype_relations=sorted(rels),
                confidence=min(0.49, 0.10 + 0.05 * len(members))))

        # synthesize value-cluster abstractions
        cluster_idx = 0
        seen_clusters: set[frozenset] = set()
        for (rel, vals), members in val_groups.items():
            # collapse duplicates (same values for same relation)
            key = frozenset(vals + [rel])
            if key in seen_clusters:
                continue
            seen_clusters.add(key)
            # collect unique members (subjects that have these values)
            unique_members = sorted(set(members))
            if len(unique_members) < self.min_members:
                continue
            name = f"proto:value_cluster_{cluster_idx}"
            cluster_idx += 1
            abstractions.append(Abstraction(
                name=name,
                kind="value_cluster",
                members=unique_members,
                prototype_relations=[rel],
                confidence=min(0.49, 0.10 + 0.05 * len(unique_members))))

        # emit membership edges as derived facts
        n_added = 0
        if not dry_run and abstractions:
            ts = iso(_now())
            for ab in abstractions:
                for member in ab.members:
                    fid = new_id()
                    self.store.conn.execute(
                        "INSERT INTO facts "
                        "(id, subject, relation, value, valid_from, "
                        " tx_from, confidence, user_id, memory_type, "
                        " is_derived, is_active, birth_commit, provenance) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (fid, ab.name, "member_of", member, ts, ts,
                         ab.confidence, user_id or "default",
                         "long_term", 1, 1,
                         commit_id,
                         _prov_json(ab)))
                    n_added += 1
            if commit_id:
                self.store.update_commit_n_facts(commit_id, n_added)

        return AbstractionResult(
            abstractions=abstractions,
            membership_edges_added=n_added,
            duration_ms=(time.perf_counter() - t0) * 1000.0)


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _prov_json(ab: Abstraction) -> str:
    import json
    return json.dumps({
        "kind": "abstraction",
        "abstraction_kind": ab.kind,
        "abstraction_name": ab.name,
        "n_members": len(ab.members),
        "prototype_relations": ab.prototype_relations,
        "generated_by": "cognition.abstraction",
    })


__all__ = ["AbstractionEngine", "Abstraction", "AbstractionResult"]
