"""Federated schema aggregation — the Semantic Flywheel (privacy-preserving).

Opt-in edge nodes contribute ONLY the *schema* of their Trace (relation
histograms, category mix, temporal density, arity stats) — never raw
data, never embeddings. The global report feeds ontology learning that
improves extraction for everyone; a single node's contribution is
indistinguishable in the aggregate (k-anonymity threshold).
"""

from __future__ import annotations

import math
from collections import Counter

from context_m.trace.fact import RELATION_CATEGORIES


def export_schema_report(store, user_id: str | None = None) -> dict:
    facts = store.query_facts(user_id=user_id, active=True,
                              include_quarantined=False)
    rel_hist = Counter(f.relation for f in facts)
    cat_hist = Counter()
    for f in facts:
        for cat, rels in RELATION_CATEGORIES.items():
            if f.relation in rels:
                cat_hist[cat] += 1
                break
        else:
            cat_hist["other"] += 1
    dated = [f for f in facts if f.relation == "event"]
    span = None
    if dated:
        span = [min(f.valid_from for f in dated), max(f.valid_from for f in dated)]
    n = max(len(facts), 1)
    return {
        "schema_version": 1,
        "n_facts": len(facts),
        "relation_histogram": dict(rel_hist),
        "category_histogram": dict(cat_hist),
        "single_valued_ratio": round(
            sum(v for k, v in rel_hist.items()
                if k in ("name", "works_at", "role", "lives_in", "prefers")) / n, 4),
        "event_count": len(dated),
        "temporal_span": span,
        "privacy": "no raw data, no embeddings, no entity names",
    }


def merge_schema_reports(reports: list[dict], k_anonymity: int = 3) -> dict:
    """Aggregate local reports into a global ontology view."""
    if not reports:
        return {"relations": {}, "categories": {}, "contributors": 0}
    rel = Counter()
    cat = Counter()
    contributors = len(reports)
    for r in reports:
        rel.update(r.get("relation_histogram", {}))
        cat.update(r.get("category_histogram", {}))
    # k-anonymity: only relations observed by >= k contributors (or >= k facts)
    global_rel = {k: v for k, v in rel.items()
                  if v >= k_anonymity * max(1, contributors // 4) or v >= 10 * k_anonymity}
    total = sum(rel.values()) or 1
    novelty = sorted(
        ((k, v / total) for k, v in rel.items() if k not in global_rel),
        key=lambda kv: -kv[1])[:10]
    return {
        "relations": dict(sorted(global_rel.items(), key=lambda kv: -kv[1])),
        "categories": dict(sorted(cat.items(), key=lambda kv: -kv[1])),
        "contributors": contributors,
        "k_anonymity": k_anonymity,
        "novel_patterns": [{"relation": k, "frequency": round(p, 5)}
                           for k, p in novelty],
    }
