"""Personalized PageRank read mode (HippoRAG 2 lineage).

The default reader treats retrieval as ranking; multi-hop questions
("what language does the team of Alice's manager use?") need GRAPH
diffusion: evidence two hops away should inherit activation mass from
the query-matched seeds. HippoRAG (arXiv:2502.14802) showed PPR over
an entity-fact graph is the neuro-symbolic analogue of hippocampal
spreading activation.

Design notes:
  * The graph is built LOCALLY from the candidate facts of one query
    (bounded), not globally — μ=0 intact, no offline index needed.
  * Deterministic: nodes are sorted, power iteration runs a fixed
    number of steps; floats do not depend on dict order.
  * Blended into fusion as an additive boost, gated by
    ``config.ppr_enabled`` (default on — it only fires for
    multihop/recall intents).
"""

from __future__ import annotations


def build_fact_graph(facts: list) -> tuple[dict[str, list[str]], set[str]]:
    """Bipartite graph: entity nodes <-> fact nodes.

    Edges: entity -- fact (subject), fact -- entity (value), plus
    fact -- fact (CONTRADICTS / TEMPORALLY_PRECEDED_BY already live in
    the trace; the caller passes edges separately).
    Returns (adjacency, fact_node_ids).
    """
    adj: dict[str, list[str]] = {}
    fact_ids: set[str] = set()

    def add_edge(a: str, b: str) -> None:
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)

    for f in facts:
        fact_ids.add(f.id)
        if f.subject:
            add_edge(f"e:{f.subject}", f.id)
        if f.value:
            add_edge(f.id, f"e:{f.value}")
    return adj, fact_ids


def personalized_pagerank(adj: dict[str, list[str]], seeds: list[str],
                          damping: float = 0.85, iters: int = 12,
                          ) -> dict[str, float]:
    """Power iteration with teleport ONLY to seed nodes.

    Deterministic: iteration visits nodes in sorted order.
    """
    nodes = sorted(adj.keys())
    if not nodes:
        return {}
    seed_set = {s for s in seeds if s in adj}
    if not seed_set:
        return {}
    n = len(nodes)
    rank = {u: (1.0 / len(seed_set)) if u in seed_set else 0.0 for u in nodes}
    teleport = {u: (1.0 / len(seed_set)) if u in seed_set else 0.0 for u in nodes}
    out_deg = {u: max(1, len(adj[u])) for u in nodes}
    for _ in range(iters):
        nxt = {u: 0.0 for u in nodes}
        base = (1.0 - damping)
        for u in nodes:
            nxt[u] += base * teleport[u]
        for u in nodes:  # sorted order → deterministic float summation
            share = damping * rank[u] / out_deg[u]
            if share == 0.0:
                continue
            for v in adj[u]:
                nxt[v] += share
        rank = nxt
    return rank


def ppr_boost(facts: list, seed_ids: list[str], edges: list[dict] | None = None,
              damping: float = 0.85, iters: int = 12) -> dict[str, float]:
    """PPR over the local fact graph. Returns {fact_id: normalized mass}.

    ``edges`` are trace edges among the given facts (dicts with src/dst);
    they connect fact nodes directly (contradiction chains etc.).
    """
    if not facts:
        return {}
    adj, fact_ids = build_fact_graph(facts)
    if edges:
        for e in edges:
            s, d = e.get("src"), e.get("dst")
            if s in fact_ids and d in fact_ids:
                adj.setdefault(s, []).append(d)
                adj.setdefault(d, []).append(s)
    seeds = [fid for fid in seed_ids if fid in adj]
    if not seeds:
        return {}
    rank = personalized_pagerank(adj, seeds, damping, iters)
    raw = {fid: rank.get(fid, 0.0) for fid in fact_ids}
    mx = max(raw.values(), default=0.0)
    if mx <= 0:
        return {}
    # normalize to [0, 1]; seeds stay near 1.0, two-hop evidence decays
    return {fid: v / mx for fid, v in raw.items() if v > 0}
