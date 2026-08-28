"""TiMem Temporal Memory Tree — 4-level consolidation hierarchy.

arXiv:2601.02845 (TiMem, ACL 2026 Findings) implements a Temporal Memory
Tree (TMT) with 5 levels: segment → session → day → week → persona.
It achieves 75.30% on LoCoMo and 76.88% on LongMemEval-S with a
52.20% reduction in recalled memory length.

Context-M's bi-temporal Trace already has the raw temporal scaffolding
(valid_from / valid_to / tx_from / tx_to windows + EXTRACTED_FROM edges).
This module adds the HIERARCHICAL ABSTRACTION layer:

  L1 (episodic)  : raw fact triples (existing)
  L2 (session)   : per-session summary fact, derived from L1 facts
                   sharing the same user_id + run_id
  L3 (day)       : per-day-per-user pattern fact, derived from L2
                   summaries sharing the same valid_from date
  L4 (persona)   : per-user stable trait fact, derived from L3 across
                   >= persona_min_sessions distinct sessions

Each higher-level fact is a DERIVED fact (is_derived=True) linked to
its constituents via DERIVED_FROM edges. The original facts remain
active — the hierarchy is an OVERLAY, not a replacement.

Retrieval benefits:
  * Simple fact lookup → L1 (existing behavior)
  * "What has Carol been up to?" → L2/L3 session/day summaries
  * "What kind of person is Carol?" → L4 persona traits
  * Tokens injected into LLM context drop ~50% because higher levels
    compress multiple L1 facts into one summary.

The summaries are written as natural-language sentences so they're
embeddable and lexically matchable by the existing palace + reader.

μ=0 SAFE — the summary generation is rule-based:
  * For a session with N works_at facts, the summary is:
        "<user> worked at <V1>, <V2>, ..., <VN> in this session"
  * For a day's L2 summaries, the summary aggregates the unique
    relations and their top values.
  * For persona, the most-reinforced stable traits (access_count > 3)
    are surfaced.

No LLM call. The NL strings are templated and deterministic.
"""
from __future__ import annotations

import datetime as _dt
from collections import defaultdict
from datetime import datetime, timezone

from cortexm.trace.edges import REFERS_TO
from cortexm.trace.fact import make_fact
from cortexm.util import iso


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Edge kind for DERIVED_FROM — the existing trace/edges.py defines
# CAUSAL/REFERS_TO/MERGED_WITH/RETRACTED_BY/CONTRADICTS/PRECEDED_BY.
# We use REFERS_TO for the downward link (summary refers to constituents)
# and the existing EXTRACTED_FROM for the upward link (constituent →
# summary, mirroring raw_chunk → fact).


def _summarize_session_facts(facts: list, user_id: str,
                              run_id: str | None) -> str:
    """Render a session summary NL string from its L1 fact set."""
    relations = defaultdict(set)
    for f in facts:
        if f.is_derived:
            continue
        relations[f.relation].add(f.value)
    parts = []
    for rel, vals in relations.items():
        vals_str = ", ".join(sorted(vals)[:5])
        if len(vals) > 5:
            vals_str += f" (+{len(vals)-5} more)"
        parts.append(f"{rel}: {vals_str}")
    summary = f"session summary for user {user_id}"
    if run_id:
        summary += f" (run {run_id})"
    summary += " — " + "; ".join(parts) if parts else summary + " (no facts)"
    return summary[:500]  # cap to keep palace embeddings focused


def _summarize_day_facts(session_summaries: list, user_id: str,
                          day_str: str) -> str:
    """Render a daily summary NL string from its L2 session summaries."""
    # session summaries are derived facts; their `value` is the NL string
    parts = [f.value for f in session_summaries if f.value]
    if not parts:
        return f"day summary for user {user_id} on {day_str} (no sessions)"
    summary = (f"day summary for user {user_id} on {day_str} — "
               f"{len(parts)} session(s). Highlights: "
               + " | ".join(parts[:3]))
    if len(parts) > 3:
        summary += f" (+{len(parts)-3} more sessions)"
    return summary[:800]


def _summarize_persona_facts(day_summaries: list, user_id: str,
                              all_facts: list) -> str:
    """Render a persona summary NL string from L3 day summaries + L1
    stable traits.

    Persona = the most-reinforced, longest-lived facts about the user.
    We pick facts with:
      * access_count >= 3 (retrieved multiple times → behaviorally
        relevant)
      * memory_type == "long_term"
      * is_active == True
    """
    # stable traits from L1
    stable = [f for f in all_facts
              if not f.is_derived
              and f.user_id == user_id
              and f.memory_type == "long_term"
              and f.is_active
              and f.access_count >= 3]
    # group by relation, take most-accessed value per relation
    by_rel = defaultdict(list)
    for f in stable:
        by_rel[f.relation].append(f)
    traits = []
    for rel, fs in by_rel.items():
        fs.sort(key=lambda x: -x.access_count)
        top = fs[0]
        traits.append(f"{rel}={top.value} (accessed {top.access_count}x)")
    summary = f"persona profile for user {user_id} — "
    summary += "; ".join(traits[:10])
    if len(traits) > 10:
        summary += f" (+{len(traits)-10} more traits)"
    return summary[:1000]


def _session_key(f) -> tuple:
    """Group facts by (user_id, run_id) for L2 clustering."""
    return (f.user_id, f.run_id or "_no_run")


def _day_key(f) -> str:
    """Group facts by user_id + date portion of valid_from for L3."""
    vf = (f.valid_from or "")[:10]  # "YYYY-MM-DD"
    return f"{f.user_id}:{vf or '_no_date'}"


def tmt_build(store, palace=None, *,
              session_cluster_mins: int = 5,
              persona_min_sessions: int = 3,
              user_id: str | None = None,
              dry_run: bool = False) -> dict:
    """Build the 4-level TiMem hierarchy as derived facts + edges.

    1. Group active, non-derived L1 facts by (user_id, run_id) →
       emit one L2 session-summary fact per group with >= N facts.
    2. Group L2 summaries by (user_id, date) → emit one L3 day-summary
       fact per (user, day) with >= 1 session.
    3. For each user with >= persona_min_sessions distinct sessions,
       emit one L4 persona-summary fact.

    Each higher-level fact is stored as is_derived=True and linked to
    its constituents via REFERS_TO edges (downward) — bi-temporal safe,
    idempotent (we check for existing derived facts with the same
    `provenance.tmt_key` before re-emitting).

    Returns a stats dict.
    """
    stats = {
        "l2_sessions_built": 0,
        "l3_days_built": 0,
        "l4_personas_built": 0,
        "l2_skipped_small": 0,
        "l4_skipped_few_sessions": 0,
        "dry_run": dry_run,
    }

    # --- load L1 facts (active, non-derived) ---------------------------
    where = "is_active=1 AND quarantined=0 AND is_derived=0"
    args: tuple = ()
    if user_id is not None:
        where += " AND user_id=?"
        args = (user_id,)
    rows = store.conn.execute(
        f"SELECT id FROM facts WHERE {where}", args).fetchall()
    fact_ids = [r[0] for r in rows]
    l1_facts = store.get_facts(fact_ids)
    if not l1_facts:
        return stats

    # open one batch commit for the whole TMT build — idempotent inserts
    # use the existing provenance.tmt_key check before re-emitting, so
    # calling tmt_build repeatedly is safe.
    if not dry_run:
        store.begin_batch()
        commit = store.create_commit(
            f"tmt_build: hierarchy pass for "
            f"{'user='+user_id if user_id else 'all users'}",
            n_facts=0)
    else:
        commit = None

    # --- L2: per-session summaries -------------------------------------
    sessions: dict[tuple, list] = defaultdict(list)
    for f in l1_facts:
        sessions[_session_key(f)].append(f)

    l2_facts: list = []
    for (uid, rid), group in sessions.items():
        if len(group) < session_cluster_mins:
            stats["l2_skipped_small"] += 1
            continue
        nl = _summarize_session_facts(group, uid, rid if rid != "_no_run" else None)
        # idempotency: skip if a derived fact with this tmt_key exists
        existing = store.conn.execute(
            "SELECT id FROM facts WHERE is_derived=1 AND "
            "provenance LIKE ? AND user_id=?",
            (f'"tmt_key":"l2:{uid}:{rid}"%', uid)).fetchall()
        if existing:
            continue
        if dry_run:
            stats["l2_sessions_built"] += 1
            continue
        f = make_fact(
            subject=uid, relation="session_summary", value=nl,
            user_id=uid, agent_id=None, run_id=(rid if rid != "_no_run" else None),
            confidence=0.85, memory_type="long_term",
            valid_from=iso(_now())[:10], now=_now(),
            provenance={"tmt_level": "L2", "tmt_key": f"l2:{uid}:{rid}",
                        "tmt_constituents": [g.id for g in group[:50]],
                        "tmt_constituent_count": len(group)},
            is_derived=True,
        )
        f.birth_commit = commit
        store.insert_fact(f, commit)
        # wire downward edges
        for g in group[:50]:  # cap edges for storage
            store.add_edge(f.id, g.id, REFERS_TO,
                           {"tmt": "l2_constituent"})
        l2_facts.append(f)
        stats["l2_sessions_built"] += 1

    # --- L3: per-day summaries ----------------------------------------
    days: dict[str, list] = defaultdict(list)
    for f in l2_facts:
        days[_day_key(f)].append(f)
    # also include standalone L1 facts that didn't get an L2 (small sessions)
    # so day summaries still cover them
    for f in l1_facts:
        days[_day_key(f)].append(f)

    l3_facts: list = []
    user_days: dict[str, set] = defaultdict(set)
    for day_key, group in days.items():
        uid = day_key.split(":", 1)[0]
        day_str = day_key.split(":", 1)[1] if ":" in day_key else "_no_date"
        l2_in_group = [g for g in group if getattr(g, "is_derived", False)
                       and getattr(g, "provenance", {}).get("tmt_level") == "L2"]
        if not l2_in_group:
            continue
        nl = _summarize_day_facts(l2_in_group, uid, day_str)
        existing = store.conn.execute(
            "SELECT id FROM facts WHERE is_derived=1 AND "
            "provenance LIKE ? AND user_id=?",
            (f'"tmt_key":"l3:{uid}:{day_str}"%', uid)).fetchall()
        if existing:
            continue
        if dry_run:
            stats["l3_days_built"] += 1
            continue
        f = make_fact(
            subject=uid, relation="day_summary", value=nl,
            user_id=uid, valid_from=day_str, now=_now(),
            confidence=0.80, memory_type="long_term",
            provenance={"tmt_level": "L3",
                        "tmt_key": f"l3:{uid}:{day_str}",
                        "tmt_constituents": [g.id for g in l2_in_group[:50]],
                        "tmt_constituent_count": len(l2_in_group)},
            is_derived=True,
        )
        f.birth_commit = commit
        store.insert_fact(f, commit)
        for g in l2_in_group[:50]:
            store.add_edge(f.id, g.id, REFERS_TO,
                           {"tmt": "l3_constituent"})
        l3_facts.append(f)
        user_days[uid].add(day_str)
        stats["l3_days_built"] += 1

    # --- L4: per-user persona summaries ------------------------------
    all_users = set([f.user_id for f in l1_facts])
    for uid in all_users:
        if user_id is not None and uid != user_id:
            continue
        if len(user_days.get(uid, set())) < persona_min_sessions:
            stats["l4_skipped_few_sessions"] += 1
            continue
        # all L1 facts for this user, sorted by reinforcement
        user_facts = [f for f in l1_facts if f.user_id == uid]
        nl = _summarize_persona_facts(
            [f for f in l3_facts if f.user_id == uid], uid, user_facts)
        existing = store.conn.execute(
            "SELECT id FROM facts WHERE is_derived=1 AND "
            "provenance LIKE ? AND user_id=?",
            (f'"tmt_key":"l4:{uid}"%', uid)).fetchall()
        if existing:
            continue
        if dry_run:
            stats["l4_personas_built"] += 1
            continue
        f = make_fact(
            subject=uid, relation="persona_summary", value=nl,
            user_id=uid, valid_from=iso(_now())[:10], now=_now(),
            confidence=0.75, memory_type="long_term",
            provenance={"tmt_level": "L4",
                        "tmt_key": f"l4:{uid}",
                        "tmt_constituent_days": list(user_days[uid])[:50],
                        "tmt_constituent_count": len(user_days[uid])},
            is_derived=True,
        )
        f.birth_commit = commit
        store.insert_fact(f, commit)
        for d in l3_facts:
            if d.user_id == uid:
                store.add_edge(f.id, d.id, REFERS_TO,
                               {"tmt": "l4_constituent"})
        stats["l4_personas_built"] += 1

    if not dry_run:
        store.end_batch()

    return stats


__all__ = ["tmt_build"]
