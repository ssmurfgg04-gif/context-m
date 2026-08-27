"""Interference-aware lifecycle — promotion, decay, consolidation.

From the August-2026 memory research (Dual-Layer Agentic Memory,
Controlled Memory Interference, LiveMem): facts are not just stored —
before commitment each candidate is evaluated for how it *interacts*
with existing memory; retention is shaped by reinforcement, recency
and interference rather than a pure Ebbinghaus curve.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from context_m.trace.contradictions import Action, Conflict, find_conflicts
from context_m.trace.fact import Fact
from context_m.trace.store import TraceStore
from context_m.util import parse_ts, similarity


@dataclass
class LifecycleDecision:
    action: Action
    commit_fact: bool = True
    target_ids: list[str] | None = None
    quarantine: bool = False
    note: str = ""
    interference: float = 0.0


def assess(store: TraceStore, candidate: Fact,
           value_match_threshold: float = 0.92) -> LifecycleDecision:
    """Pre-commit interference evaluation (Controlled Memory Interference)."""
    conflict = find_conflicts(store, candidate)
    inter = 0.0

    if conflict.action is Action.SUPERSEDE:
        old = conflict.existing[0]
        # semantic interference: how much the new fact disrupts the old one
        inter = similarity(old.value, candidate.value)
        note = conflict.note
        if inter > 0.75 and candidate.confidence < 0.6:
            note += "; low-confidence supersession flagged for audit"
        return LifecycleDecision(
            action=Action.SUPERSEDE, commit_fact=True,
            target_ids=[f.id for f in conflict.existing],
            note=note, interference=inter)

    if conflict.action is Action.MERGE:
        return LifecycleDecision(
            Action.MERGE, commit_fact=False,
            target_ids=[f.id for f in conflict.existing],
            note=conflict.note, interference=1.0)

    if conflict.action is Action.SKIP:
        return LifecycleDecision(
            Action.SKIP, commit_fact=False,
            target_ids=[f.id for f in conflict.existing],
            note=conflict.note)

    if conflict.action is Action.COEXIST:
        # interference over a bounded recent sample (linear at scale)
        if candidate.relation == "mentioned":
            inter = 0.0
        else:
            recent = conflict.existing[-8:]
            inter = max((similarity(f.value, candidate.value) for f in recent),
                        default=0.0)
        return LifecycleDecision(
            Action.COEXIST, commit_fact=True, interference=inter,
            note=conflict.note)

    return LifecycleDecision(Action.COMMIT, commit_fact=True, note="new fact")


def retention_score(fact: Fact, now: _dt.datetime) -> float:
    """Interference-aware retention: conf x reinforcement x recency decay,
    reduced by contradiction pressure (number of supersessions the
    subject-relation chain went through)."""
    age_days = max(
        0.0, (now - (parse_ts(fact.tx_from) or now)).total_seconds() / 86400.0)
    recency = 1.0 / (1.0 + age_days / 30.0) ** 0.5
    reinforce = 1.0 + 0.5 * (fact.reinforcement - 1) + 0.1 * min(fact.access_count, 20)
    pressure = 1.0 + 0.25 * fact.provenance.get("chain_updates", 0)
    return fact.confidence * recency * reinforce / pressure


def consolidate(store: TraceStore, now: _dt.datetime | None = None,
                promote_reinforcement: int = 2,
                demote_threshold: float = 0.15) -> dict:
    """Fast-write routing + slow consolidation (Dual-Layer Agentic Memory):
    promote reinforced short-term facts, decay untouched ones."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    promoted, demoted, deactivated = 0, 0, 0
    for f in store.query_facts(active=True, derived=None):
        if f.is_derived:
            continue
        score = retention_score(f, now)
        if f.memory_type == "short_term" and (
                f.reinforcement >= promote_reinforcement or f.access_count >= 3) \
                and score >= 0.25:
            store.update_fact(f.id, memory_type="long_term")
            promoted += 1
        elif f.memory_type == "short_term" and score < demote_threshold:
            store.update_fact(f.id, is_active=0,
                              provenance={**f.provenance, "deactivated": "decay"})
            deactivated += 1
        elif f.memory_type == "long_term" and score < demote_threshold * 0.5:
            demoted += 1
            store.update_fact(f.id, memory_type="short_term",
                              provenance={**f.provenance, "demoted": "decay"})
    return {"promoted": promoted, "demoted": demoted, "deactivated": deactivated}
