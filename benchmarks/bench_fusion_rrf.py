"""Fusion bench: weighted-sum (current) vs RRF (Cormack k=60) on identical inputs.

Corpus: pattern-friendly messages ingested via the real writer path.
Queries: judged (lexical / paraphrase / mixed classes), gold = fact IDs
resolved by (subject, relation, value) lookup after ingest.

Method A mirrors reader.search fusion weights (0.6 VSA + 0.35 chunk,
mentioned-damp 0.45) minus symbolic/graph/prefetch/rerank.
Method B fuses the SAME two ranked lists with RRF: sum(1/(60+rank)).

Metrics: recall@5, MRR, mean latency per method, overall + per class.
Exit 0 always; prints a verdict line (no auto-promotion of either side).

Usage:  python benchmarks/bench_fusion_rrf.py
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, '.')

from cortexm import mount_default

USER = "bench"
RRF_K = 60

MESSAGES = [
    # employment
    "Alice works at Google.",
    "Bob works at Stripe.",
    "Carol works at Anthropic.",
    "Dave works at Google.",
    "Eve left Microsoft in March.",
    "Frank works at Stripe.",
    # prefs
    "Alice prefers tea.",
    "Bob prefers coffee.",
    "Carol likes hiking.",
    "Dave prefers tea.",
    "Eve likes sailing.",
    # skills
    "Alice knows rust.",
    "Bob knows python.",
    "Carol knows go.",
    "Dave knows rust.",
    # lives_in
    "Alice lives in Toronto.",
    "Bob lives in Berlin.",
    "Carol lives in Toronto.",
    "Eve lives in Paris.",
    # events / misc proclivity
    "Alice visited Paris in June.",
    "Bob visited Tokyo in April.",
    "Carol runs every morning.",
    "Dave plays chess on weekends.",
    "Eve bakes sourdough bread.",
    "Frank cycles to work daily.",
    # near-duplicate distractors (ambiguity pressure)
    "Alice likes coffee too.",
    "Bob drinks tea in the morning.",
    "Carol prefers coffee over tea.",
    "Dave lives in Berlin now.",
    "Someone mentioned Toronto traffic today.",
]

# (query, class, [(subject, relation, value-substring), ...])
QUERIES = [
    ("Where does Alice work?", "lexical", [("Alice", "works_at", "Google")]),
    ("What does Bob prefer?", "lexical", [("Bob", "prefers", "coffee")]),
    ("Where does Carol live?", "lexical", [("Carol", "lives_in", "Toronto")]),
    ("What does Dave know?", "lexical", [("Dave", "knows", "rust")]),
    ("Which company employs Alice?", "paraphrase", [("Alice", "works_at", "Google")]),
    ("What beverage does Bob like?", "paraphrase", [("Bob", "prefers", "coffee")]),
    ("Where does Carol reside?", "paraphrase", [("Carol", "lives_in", "Toronto")]),
    ("Which programming language does Bob know?", "paraphrase", [("Bob", "knows", "python")]),
    ("Who works at Google?", "mixed", [("Alice", "works_at", "Google"), ("Dave", "works_at", "Google")]),
    ("Who prefers tea?", "mixed", [("Alice", "prefers", "tea"), ("Dave", "prefers", "tea")]),
    ("What did Eve leave?", "mixed", [("Eve", "left", "Microsoft")]),
    ("Who lives in Berlin?", "mixed", [("Bob", "lives_in", "Berlin"), ("Dave", "lives_in", "Berlin")]),
]


def build():
    ctx = mount_default(db_path=":memory:")
    mem = ctx.inject("memory")["memory"]
    for m in MESSAGES:
        mem.add(m, user_id=USER)
    return mem


def gold_ids(mem, triples):
    out = []
    for subj, rel, val in triples:
        rows = mem.store.conn.execute(
            "SELECT id FROM facts WHERE user_id=? AND is_active=1 "
            "AND subject LIKE ? AND relation LIKE ? AND value LIKE ?",
            (USER, f"%{subj}%", f"%{rel}%", f"%{val}%")).fetchall()
        out.extend(r[0] for r in rows)
    return out


def signals(mem, query, scope, k=50):
    """The two ranked signals both methods share."""
    qvec = mem.palace.embedder.embed(query)
    vsa = mem.palace.search(qvec, k, candidate_ids=set(scope))
    cscores, _ = mem.reader._chunk_recall(
        query, qvec, set(scope), USER, None, None, None)
    # chunk order -> fact order (first-seen wins, in chunk-rank order)
    chunk_order = sorted(cscores, key=lambda c: -cscores[c])
    chunk_facts = []
    seen = set()
    for cid in chunk_order:
        for f in mem.store.facts_for_chunk(cid, active_only=True):
            if f.id in scope and f.id not in seen:
                seen.add(f.id)
                chunk_facts.append(f.id)
    return vsa, chunk_facts, cscores


def rank_weighted(mem, vsa, chunk_facts, cscores):
    cands: dict[str, float] = {}
    for fid, s in vsa:
        f = mem.store.get_fact(fid)
        damp = 0.45 if (f and f.relation == "mentioned") else 1.0
        cands[fid] = cands.get(fid, 0.0) + 0.6 * max(0.0, s) * damp
    for fid in chunk_facts:
        cands[fid] = cands.get(fid, 0.0) + 0.35 * cscores.get(
            _chunk_of(mem, fid), 0.0)
    return sorted(cands, key=lambda f: -cands[f])


def _chunk_of(mem, fid):
    f = mem.store.get_fact(fid)
    return f.source_id if f else None


def rank_rrf(vsa, chunk_facts, k=RRF_K):
    scores: dict[str, float] = {}
    for rank, (fid, _s) in enumerate(vsa, start=1):
        scores[fid] = scores.get(fid, 0.0) + 1.0 / (k + rank)
    for rank, fid in enumerate(chunk_facts, start=1):
        scores[fid] = scores.get(fid, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda f: -scores[f])


def metrics(ranked, gold, k=5):
    top = ranked[:k]
    hits = [g for g in gold if g in top]
    rec = len(hits) / max(1, len(gold))
    mrr = 0.0
    for g in gold:
        if g in ranked:
            mrr = max(mrr, 1.0 / (ranked.index(g) + 1))
    return rec, mrr


def main() -> None:
    mem = build()
    scope = [r[0] for r in mem.store.conn.execute(
        "SELECT id FROM facts WHERE user_id=? AND is_active=1",
        (USER,)).fetchall()]
    print(f"corpus: {len(MESSAGES)} msgs, {len(scope)} facts, "
          f"{len(QUERIES)} queries")
    agg = {m: {"rec": [], "mrr": [], "ms": []} for m in ("A", "B")}
    per_class: dict[str, dict[str, list[float]]] = {}
    for query, cls, triples in QUERIES:
        gold = gold_ids(mem, triples)
        if not gold:
            print(f"SKIP (no gold found): {query}")
            continue
        vsa, chunk_facts, cscores = signals(mem, query, scope)
        t0 = time.perf_counter()
        ra = rank_weighted(mem, vsa, chunk_facts, cscores)
        ta = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        rb = rank_rrf(vsa, chunk_facts)
        tb = (time.perf_counter() - t0) * 1000
        ra_rec, ra_mrr = metrics(ra, gold)
        rb_rec, rb_mrr = metrics(rb, gold)
        agg["A"]["rec"].append(ra_rec)
        agg["A"]["mrr"].append(ra_mrr)
        agg["A"]["ms"].append(ta)
        agg["B"]["rec"].append(rb_rec)
        agg["B"]["mrr"].append(rb_mrr)
        agg["B"]["ms"].append(tb)
        per_class.setdefault(cls, {"A": [], "B": []})
        per_class[cls]["A"].append(ra_rec)
        per_class[cls]["B"].append(rb_rec)
        flag = "" if ra_rec == rb_rec else ("  <-- DIFFERS" if rb_rec > ra_rec else "  <-- weighted wins")
        print(f"[{cls:10s}] R@5 A={ra_rec:.2f} B={rb_rec:.2f} | "
              f"MRR A={ra_mrr:.2f} B={rb_mrr:.2f}{flag} :: {query}")
    for m in ("A", "B"):
        n = max(1, len(agg[m]["rec"]))
        print(f"method {m}: recall@5={sum(agg[m]['rec'])/n:.3f} "
              f"MRR={sum(agg[m]['mrr'])/n:.3f} "
              f"mean_ms={sum(agg[m]['ms'])/n:.2f}")
    for cls, d in per_class.items():
        a = sum(d["A"]) / max(1, len(d["A"]))
        b = sum(d["B"]) / max(1, len(d["B"]))
        print(f"  class {cls}: weighted={a:.3f} rrf={b:.3f}")
    a = sum(agg["A"]["rec"]) / max(1, len(agg["A"]["rec"]))
    b = sum(agg["B"]["rec"]) / max(1, len(agg["B"]["rec"]))
    if b > a:
        print("VERDICT: RRF wins on recall — candidate for reader behind flag")
    elif b == a:
        print("VERDICT: tie — RRF simpler (no tuned weights), consider it")
    else:
        print("VERDICT: weighted wins here — keep current fusion")


if __name__ == "__main__":
    main()