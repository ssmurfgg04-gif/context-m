"""Deterministic contradiction detection & truth maintenance.

Phase rules from Section 1.1:
  1. Classification      — relation → semantic category, single/multi valued
  2. Contradiction       — exact + fuzzy (Jaccard/Levenshtein) match on
                           subject-relation pairs; latest-value-wins for
                           single-valued relations, append for multi-valued
  3. Interference-aware lifecycle — see lifecycle.py
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from cortexm.trace.fact import Fact, SINGLE_VALUED
from cortexm.trace.store import TraceStore
from cortexm.util import similarity


class Action(str, Enum):
    COMMIT = "commit"          # brand-new fact
    MERGE = "merge"            # near-duplicate: reinforce existing
    SUPERSEDE = "supersede"    # contradiction on single-valued relation
    COEXIST = "coexist"        # contradiction on multi-valued relation
    SKIP = "skip"              # exact duplicate


@dataclass
class Conflict:
    action: Action
    existing: list[Fact]
    note: str = ""


def find_conflicts(store: TraceStore, candidate: Fact) -> Conflict:
    """Decide how ``candidate`` interacts with active memory."""
    existing = [
        f for f in store.query_facts(
            subject=candidate.subject, relation=candidate.relation,
            user_id=candidate.user_id, active=True)
        if f.id != candidate.id
    ]
    if not existing:
        return Conflict(Action.COMMIT, [])

    exact = [f for f in existing if f.value.strip().lower() == candidate.value.strip().lower()]
    if exact:
        return Conflict(Action.SKIP, exact, "exact duplicate")

    # low-salience mention anchors: exact-dup semantics only (no fuzzy
    # quadratic scans — mention streams are high-volume, low-signal)
    if candidate.relation in ("mentioned", "event", "instruction"):
        if candidate.relation == "mentioned":
            return Conflict(Action.COEXIST, existing, "mention anchor recorded")

    near = [f for f in existing
            if similarity(f.value, candidate.value) >= 0.92]
    if near:
        return Conflict(Action.MERGE, near, "near-duplicate merged; reinforcement +1")

    single = candidate.relation in SINGLE_VALUED
    if single:
        # newest valid_from wins reality; old fact gets valid_to
        target = max(existing, key=lambda f: (f.valid_from, f.tx_from))
        return Conflict(Action.SUPERSEDE, [target],
                        f"contradiction on single-valued '{candidate.relation}'")
    return Conflict(Action.COEXIST, existing,
                    f"conflicting values on multi-valued '{candidate.relation}' coexist")
