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


# v0.6.1: relation-name aliasing. The extractor emits several surface
# relations that all mean the same semantic thing:
#   * moved_to / relocated_to / shifted_to — all "where the user lives"
#   * joined / started_at / got_job_at    — all "where the user works"
# Without this map, "I moved to Berlin" (moved_to) and "I live in Munich"
# (lives_in) sit in DIFFERENT relation slots, so find_conflicts treats
# them as unrelated facts — the bi-temporal SUPERSEDE chain never fires
# and the reader answers "Berlin AND Munich" instead of "Munich (latest)".
# μ=0: a static dict. No model, no inference.
RELATION_ALIASES: dict[str, str] = {
    # residence cluster
    "moved_to": "lives_in",
    "relocated_to": "lives_in",
    "shifted_to": "lives_in",
    "based_in": "lives_in",
    # employment cluster
    "started_at": "works_at",
    "joined": "works_at",
    "got_job_at": "works_at",
    "hired_at": "works_at",
    # education cluster (aliasing 'study_at' / 'enrolled_at' to 'studies_at')
    "study_at": "studies_at",
    "enrolled_at": "studies_at",
}


def canonical_relation(relation: str) -> str:
    """Return the canonical relation name. Used by find_conflicts and
    the reader's symbolic query path so alias-of-alias queries land in
    the same bucket. Falls through unchanged for relations without an
    alias."""
    return RELATION_ALIASES.get(relation, relation)


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
    """Decide how ``candidate`` interacts with active memory.

    v0.6.1: relation aliasing. Before querying for existing facts on
    the same (subject, relation) we look up the canonical relation
    so a ``moved_to = Berlin`` candidate sees the prior ``lives_in =
    Munich`` and fires SUPERSEDE (not COEXIST-unknown).
    """
    canon_rel = canonical_relation(candidate.relation)
    # Look up BOTH the candidate's literal relation AND the canonical
    # form — older facts may be stored under either name. We union the
    # two queries so a partial migration (some facts pre-aliasing, some
    # post-) still resolves correctly.
    matching: list[Fact] = []
    for rel in {candidate.relation, canon_rel}:
        matching.extend(
            f for f in store.query_facts(
                subject=candidate.subject, relation=rel,
                user_id=candidate.user_id, active=True)
            if f.id != candidate.id)
    # de-dup by fact id (a fact stored under the canonical rel could
    # appear in both queries if candidate.relation == canon_rel)
    seen_ids: set[str] = set()
    existing: list[Fact] = []
    for f in matching:
        if f.id in seen_ids:
            continue
        seen_ids.add(f.id)
        existing.append(f)
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

    # SINGLE_VALUED is keyed by canonical relation name; check both
    single = (candidate.relation in SINGLE_VALUED
              or canon_rel in SINGLE_VALUED)
    if single:
        # newest valid_from wins reality; old fact gets valid_to
        target = max(existing, key=lambda f: (f.valid_from, f.tx_from))
        return Conflict(Action.SUPERSEDE, [target],
                        f"contradiction on single-valued '{canon_rel}'")
    return Conflict(Action.COEXIST, existing,
                    f"conflicting values on multi-valued '{canon_rel}' coexist")
