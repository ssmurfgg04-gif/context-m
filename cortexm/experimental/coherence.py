"""Temporal-coherence reranking signal (μ=0).

Borrowed from RuVector's temporal-coherence gating ("prefer memories
supported by related observations"), stripped to a deterministic
heuristic:

  * a fact's coherence score is the number of DISTINCT other facts,
    in the same candidate pool, whose valid_from lies within
    ``window_days`` of it AND which share an entity (subject or
    value) with it;
  * the raw count is normalized to [0, 1] (capped at 5 corroborating
    facts) and used as a small ADDITIVE boost by the reader.

Intuition: an event that really happened in a given week usually
left several mutually-connected facts around that week (the
conversation about it, related decisions, follow-ups). A fact that
only similarity-matched the query but has no temporal neighborhood
is more likely to be the WRONG session's look-alike. This targets
the temporal_reasoning failures on multi-week relative references
("two weeks after she started the new job") where pure similarity
pulls the wrong time slice.

μ=0: date parsing, set intersection, counting. No learned weights.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

try:  # Python 3.11+
    from datetime import UTC
except ImportError:  # pragma: no cover
    UTC = None


def _parse_valid_from(value: str | None) -> datetime | None:
    """Parse a fact's valid_from into a datetime, tolerantly.

    LongMemEval-style stores carry ``YYYY-MM-DD`` (sometimes with a
    time part, sometimes Z-suffixed). Returns None for anything
    unparseable — those facts simply get no coherence boost.
    """
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    # date-only fast path
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(s[:26], fmt)
        except ValueError:
            continue
    return None


def coherence_scores(facts: list[Any], window_days: int = 30,
                     cap: int = 5) -> dict[str, float]:
    """Deterministic temporal-coherence score per fact.

    Returns {fact_id: score in [0, 1]} — 0 for facts without a
    parseable valid_from. O(n log n): sort by timestamp, then a
    two-pointer window pass; shared-entity checking uses the
    entity-token sets computed once per fact.
    """
    if not facts:
        return {}
    window = timedelta(days=window_days if window_days > 0 else 30)

    # ---- parse + entity tokens (one pass) ----
    entries: list[tuple[datetime, Any, frozenset[str]]] = []
    entity_tokens: dict[str, frozenset[str]] = {}
    for f in facts:
        dt = _parse_valid_from(getattr(f, "valid_from", None))
        if dt is None:
            continue
        subj = (getattr(f, "subject", "") or "").strip().lower()
        val_toks = frozenset(
            p.strip("'\"!?;:()[]").lower()
            for p in (getattr(f, "value", "") or "").replace(",", " ") \
                .replace(".", " ").split()
            if len(p) >= 3 and p.isalnum())
        ents = frozenset(e for e in ({subj} | val_toks) if e)
        entity_tokens[f.id] = ents
        entries.append((dt, f, ents))

    scores: dict[str, float] = {f.id: 0.0 for f in facts}
    if len(entries) < 2:
        return scores

    # ---- sort by timestamp (deterministic tie-break on id) ----
    entries.sort(key=lambda e: (e[0], e[1].id))

    # ---- two-pointer window pass ----
    # For each fact i, facts j in (t_i - window, t_i + window) with a
    # shared entity contribute one corroboration each (distinct j).
    n = len(entries)
    counts: dict[str, int] = {}
    for i, (dt_i, f_i, ents_i) in enumerate(entries):
        c = counts.get(f_i.id, 0)
        # forward scan
        j = i + 1
        while j < n and entries[j][0] <= dt_i + window:
            if ents_i & entries[j][2]:
                c += 1
            j += 1
        # backward scan
        j = i - 1
        while j >= 0 and entries[j][0] >= dt_i - window:
            if ents_i & entries[j][2]:
                c += 1
            j -= 1
        counts[f_i.id] = c

    for f in facts:
        c = counts.get(f.id, 0)
        scores[f.id] = min(1.0, c / cap) if cap > 0 else 0.0
    return scores
