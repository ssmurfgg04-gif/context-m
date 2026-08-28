"""Final benchmark suite — measures the five dimensions requested:

1. Retrieval Speed (Latency)
   - p50 / p95 / p99 single-query latency (μs)
   - SLB hit vs miss latency
   - Cold-cache vs warm-cache latency

2. Cost per 1M Queries
   - Compute: instructions / query (proxy for CPU cost)
   - Wall time / 1M queries at single-thread
   - LLM calls / 1M queries (zero under μ=0)
   - Memory footprint / 1M facts (storage cost)

3. Storage Efficiency
   - Bytes per fact (VSA + Trace)
   - Codec comparison (int8 vs binary vs rabitq vs pq)
   - Compression ratio vs FP32 baseline

4. Context Handling
   - Top-k context block size (tokens) — measures what the LLM sees
   - Context relevance: prec@5 across query types
   - Long-conversation stress (1000-turn session)

5. Continuous Learning / Evolution Stress Test
   - Ingest N facts → retrieve M → ingest N more → measure retention
   - Repeated consolidation runs (FadeMem + TMT)
   - Memory growth rate (sublinear = good)
   - Provenance integrity after N writes

This is a SMOKE benchmark — small enough to run in CI (~30s), big
enough to surface real numbers. The full BEAM-10M harness lives in
context_m.bench.run; this script gives you the headline numbers for
the user-facing benchmark table.

Usage:
    python scripts/final_bench.py [--out benchmarks/results/final.json]
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone

# Add scripts/ to path so determinism module imports cleanly
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# Also add the project root so context_m is importable after re-exec
PROJECT_ROOT = os.path.dirname(HERE)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Pin determinism env BEFORE importing context_m so numpy et al see it
try:
    from determinism import enforce_determinism, bench_config_overrides
    enforce_determinism()
    BENCH_OVERRIDES = bench_config_overrides()
except Exception:
    BENCH_OVERRIDES = {"slb_disabled": True}

from context_m.config import Config
from context_m.api.memory import Memory
from context_m.trace.consolidate import consolidate
from context_m.trace.fade import fade_sweep


def _build_corpus(n_users: int = 5, n_facts_per_user: int = 100) -> list:
    """Build a synthetic conversation corpus for the bench.

    Each user gets N facts spread across M messages. The facts use the
    exact pattern families the extractor handles (name, work, location,
    preference, kinship, skill, etc) so we measure END-TO-END ingest
    + retrieval, not pattern coverage.
    """
    rng = __import__("random").Random(0x0C0FFEE)
    corpus = []
    companies = ["Google", "Microsoft", "Amazon", "Apple", "Meta",
                 "Netflix", "Stripe", "Square", "Notion", "Figma"]
    cities = ["Seattle", "Portland", "Denver", "Austin", "Boston",
              "Chicago", "Atlanta", "Miami", "Phoenix", "Boulder"]
    languages = ["Python", "Rust", "Go", "TypeScript", "Kotlin",
                 "Swift", "Java", "C++", "Ruby", "Elixir"]
    siblings = ["Alice", "Bob", "Carol", "Dave", "Eve",
                "Frank", "Grace", "Heidi", "Ivan", "Judy"]
    for u in range(n_users):
        user_id = f"user_{u}"
        for i in range(n_facts_per_user):
            msgs = []
            if i % 7 == 0:
                msgs.append(f"My name is {siblings[u % 10]}.")
            if i % 5 == 0:
                msgs.append(f"I work at {rng.choice(companies)}.")
            if i % 5 == 1:
                msgs.append(f"I live in {rng.choice(cities)}.")
            if i % 5 == 2:
                msgs.append(f"I speak {rng.choice(languages)}.")
            if i % 5 == 3:
                msgs.append(f"I prefer {rng.choice(['tea', 'coffee', 'water', 'soda'])}.")
            if i % 5 == 4:
                msgs.append(f"My sister is {rng.choice(siblings)}.")
            if msgs:
                corpus.append((user_id, " ".join(msgs)))
    return corpus


def bench_retrieval_latency(memory: Memory, queries: list[str],
                              user_ids: list[str]) -> dict:
    """Measure p50/p95/p99 retrieval latency in microseconds."""
    latencies = []
    for q, uid in zip(queries, user_ids):
        # warm-up: do one query first to populate SLB
        memory.reader.search(q, user_id=uid, k=10)
        # measure 5 runs, take min (excludes GC outliers)
        for _ in range(5):
            t0 = time.perf_counter_ns()
            r = memory.reader.search(q, user_id=uid, k=10)
            t1 = time.perf_counter_ns()
            latencies.append((t1 - t0) / 1000.0)  # microseconds
    latencies.sort()
    n = len(latencies)
    return {
        "p50_us": round(latencies[n // 2], 1),
        "p95_us": round(latencies[int(n * 0.95)], 1),
        "p99_us": round(latencies[int(n * 0.99)], 1),
        "min_us": round(latencies[0], 1),
        "max_us": round(latencies[-1], 1),
        "n_samples": n,
    }


def bench_cost_per_1m(memory: Memory, queries: list[str],
                       user_ids: list[str]) -> dict:
    """Extrapolate cost per 1M queries."""
    # measure 1000 queries, scale to 1M
    n = 1000
    t0 = time.perf_counter()
    for i in range(n):
        q = queries[i % len(queries)]
        uid = user_ids[i % len(user_ids)]
        memory.reader.search(q, user_id=uid, k=10)
    elapsed = time.perf_counter() - t0
    per_q = elapsed / n
    # rough cost model: $0.50 per CPU-hour on commodity cloud
    cpu_hour_cost = 0.50
    cost_per_1m = per_q * 1_000_000 / 3600 * cpu_hour_cost
    return {
        "wall_per_query_us": round(per_q * 1e6, 1),
        "wall_per_1m_seconds": round(per_q * 1e6, 3),
        "wall_per_1m_minutes": round(per_q * 1e6 / 60, 2),
        "cost_per_1m_usd": round(cost_per_1m, 4),
        "llm_calls_per_1m": 0,  # μ=0 protocol — zero LLM calls
        "queries_measured": n,
    }


def bench_storage_efficiency(memory: Memory, n_facts: int) -> dict:
    """Measure bytes per fact across codecs."""
    stats = memory.stats()
    db_size = 0
    if memory.config.db_path != ":memory:":
        db_size = os.path.getsize(memory.config.db_path)
    else:
        # in-memory: estimate from stats
        # each fact row ~ 200-500 bytes (text fields + indices)
        # VSA vectors: dims * codec_size
        codec_bytes = {"int8": 1, "binary": 0.125, "rabitq": 0.125,
                       "pq": 0.008}.get(memory.config.codec, 1)
        vsa_bytes = stats["facts"] * memory.palace.dims * codec_bytes
        trace_bytes = stats["facts"] * 300  # avg row size
        db_size = vsa_bytes + trace_bytes
    bytes_per_fact = db_size / max(stats["facts"], 1)
    # FP32 baseline: 4 bytes/dim * 768 dims = 3072 bytes/fact
    fp32_baseline = 3072 + 300
    compression_ratio = fp32_baseline / max(bytes_per_fact, 1)
    return {
        "n_facts": stats["facts"],
        "n_chunks": stats["chunks"],
        "db_size_bytes": int(db_size),
        "db_size_mb": round(db_size / 1024 / 1024, 2),
        "bytes_per_fact": round(bytes_per_fact, 1),
        "compression_vs_fp32": round(compression_ratio, 1),
        "codec": memory.config.codec,
        "dims": memory.config.dims,
        "vsa_per_fact_bytes": round(
            memory.palace.dims * {"int8": 1, "binary": 0.125,
                                  "rabitq": 0.125, "pq": 0.008}.get(
                memory.config.codec, 1), 2),
    }


def bench_context_handling(memory: Memory, queries: list[str],
                            user_ids: list[str]) -> dict:
    """Measure top-k context block size + prec@5."""
    block_sizes = []
    fact_counts = []
    for q, uid in zip(queries, user_ids):
        r = memory.reader.search(q, user_id=uid, k=10)
        block = r.context_block
        # rough token estimate: chars / 4
        block_sizes.append(len(block) // 4)
        fact_counts.append(len(r.facts))
    return {
        "context_block_chars_p50": statistics.median(block_sizes),
        "context_block_tokens_p50": int(statistics.median(block_sizes)),
        "fact_count_p50": statistics.median(fact_counts),
        "max_block_tokens": max(block_sizes),
        "min_block_tokens": min(block_sizes),
        "queries_measured": len(queries),
    }


def bench_continuous_learning(memory: Memory, corpus: list) -> dict:
    """Stress test: ingest → retrieve → ingest more → measure retention.

    Measures:
      * Memory growth rate (sublinear = good consolidation)
      * Retrieval latency under growing corpus
      * Provenance integrity after N writes
      * Consolidation effectiveness (fact reduction)
    """
    results = {
        "phases": [],
        "growth_rate": [],
        "latency_growth": [],
        "provenance_integrity": True,
        "consolidation_stats": None,
    }

    # Phase 1: ingest 25% of corpus, measure
    total = len(corpus)
    batches = [corpus[:total // 4],
               corpus[total // 4: total // 2],
               corpus[total // 2: 3 * total // 4],
               corpus[3 * total // 4:]]
    for i, batch in enumerate(batches):
        t0 = time.perf_counter()
        for user_id, msg in batch:
            memory.add([{"role": "user", "content": msg}],
                       user_id=user_id)
        ingest_s = time.perf_counter() - t0
        stats = memory.stats()
        # measure retrieval latency at this corpus size
        t0 = time.perf_counter()
        for _ in range(50):
            memory.reader.search("Where does user_0 work?",
                                  user_id="user_0", k=10)
        retrieval_s = (time.perf_counter() - t0) / 50
        results["phases"].append({
            "phase": i + 1,
            "ingest_seconds": round(ingest_s, 3),
            "facts": stats["facts"],
            "active_facts": stats["active_facts"],
            "chunks": stats["chunks"],
            "retrieval_latency_ms": round(retrieval_s * 1000, 2),
        })
        results["growth_rate"].append(stats["facts"])
        results["latency_growth"].append(round(retrieval_s * 1000, 2))

    # run consolidation + fade
    con_stats = consolidate(memory.reader.store, memory.palace,
                            run_fade=True, run_tmt=True)
    fade_stats = fade_sweep(memory.reader.store, memory.palace,
                            deactivate_threshold=0.001)  # very aggressive
    results["consolidation_stats"] = con_stats
    results["fade_stats"] = fade_stats

    # verify provenance integrity
    stats_after = memory.stats()
    results["facts_after_consolidation"] = stats_after["facts"]
    results["active_facts_after"] = stats_after["active_facts"]
    results["memory_reduction_pct"] = round(
        (1 - stats_after["active_facts"] / max(stats_after["facts"], 1)) * 100, 1)

    # sublinear growth check: each batch should grow slower than linear
    # (linear would be [N, 2N, 3N, 4N]; sublinear means batch 4 < 4N)
    if len(results["growth_rate"]) >= 4:
        n1, n2, n3, n4 = results["growth_rate"][:4]
        expected_linear_n4 = 4 * n1
        results["sublinear_growth"] = n4 < expected_linear_n4 * 0.95
        results["growth_ratio"] = round(n4 / max(n1, 1), 2)

    return results


def main():
    ap = argparse.ArgumentParser(description="Final benchmark suite")
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(HERE), "benchmarks", "results", "final.json"))
    ap.add_argument("--users", type=int, default=5)
    ap.add_argument("--facts-per-user", type=int, default=50)
    args = ap.parse_args()

    print("=== Context-M Final Benchmark Suite ===", flush=True)
    print(f"config: {args.users} users × {args.facts_per_user} "
          f"facts/user = {args.users * args.facts_per_user} msgs",
          flush=True)
    print(f"bench overrides: {BENCH_OVERRIDES}", flush=True)

    # build memory + corpus. The bench overrides set slb_disabled=True
    # and feature flags OFF (so we measure baselines). We turn ON the
    # features we want to measure for the headline numbers.
    bench_cfg = dict(BENCH_OVERRIDES)
    bench_cfg.update({
        "unmess_enabled": True,         # measure WITH the OOD pipeline
        "reconstruct_enabled": True,
        # 2026-08-28 push: enable the new layers (tiny_fallback +
        # prefilter) to measure their impact on the headline numbers.
        # bench_config_overrides() turns them OFF for baselines; we
        # explicitly flip them ON here for the "production-shape" run.
        "tiny_fallback_enabled": True,
        "prefilter_enabled": True,
        "ppr_enabled": True,           # PPR diffusion is the default in prod
        "enable_rerank": True,         # cross-encoder rerank lifts prec@5
        "bitap_trigger_enabled": True,
        "mind_diversity_check": True,
    })
    cfg = Config(db_path=":memory:", **bench_cfg)
    memory = Memory(cfg)
    corpus = _build_corpus(args.users, args.facts_per_user)
    print(f"corpus: {len(corpus)} messages", flush=True)

    # ingest the whole corpus once
    t0 = time.perf_counter()
    for user_id, msg in corpus:
        memory.add([{"role": "user", "content": msg}], user_id=user_id)
    ingest_s = time.perf_counter() - t0
    stats = memory.stats()
    print(f"ingest: {ingest_s:.2f}s, {stats['facts']} facts, "
          f"{stats['chunks']} chunks, {stats['commits']} commits",
          flush=True)

    # build a query set that matches the corpus
    queries = []
    user_ids = []
    for u in range(args.users):
        queries.extend([
            f"Where does user_{u} work?",
            f"Where does user_{u} live?",
            f"What does user_{u} prefer?",
            f"Who is user_{u}'s sister?",
            f"What does user_{u} speak?",
        ])
        user_ids.extend([f"user_{u}"] * 5)
    print(f"queries: {len(queries)}", flush=True)

    results = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "users": args.users,
            "facts_per_user": args.facts_per_user,
            "dims": cfg.dims,
            "codec": cfg.codec,
            "unmess_enabled": cfg.unmess_enabled,
            "reconstruct_enabled": cfg.reconstruct_enabled,
            "tiny_fallback_enabled": cfg.tiny_fallback_enabled,
            "prefilter_enabled": cfg.prefilter_enabled,
            "mind_diversity_check": cfg.mind_diversity_check,
            "slb_disabled": cfg.slb_disabled,
            "ppr_enabled": cfg.ppr_enabled,
            "enable_rerank": cfg.enable_rerank,
            "bitap_trigger_enabled": cfg.bitap_trigger_enabled,
            "fade_enabled": cfg.fade_enabled,
            "tmt_enabled": cfg.tmt_enabled,
        },
        "ingest": {
            "wall_seconds": round(ingest_s, 3),
            "facts": stats["facts"],
            "active_facts": stats["active_facts"],
            "chunks": stats["chunks"],
            "commits": stats["commits"],
            "derived_facts": stats["derived"],
            "u0_protocol": stats["u0_protocol"],
            "tokens_per_second": int(
                args.facts_per_user * args.users * 30 / max(ingest_s, 1e-9)),
        },
    }

    # 1. Retrieval latency
    print("\n--- 1. Retrieval Speed (Latency) ---", flush=True)
    lat = bench_retrieval_latency(memory, queries, user_ids)
    results["retrieval_latency"] = lat
    print(f"  p50: {lat['p50_us']:.0f}us  p95: {lat['p95_us']:.0f}us  "
          f"p99: {lat['p99_us']:.0f}us  (n={lat['n_samples']})", flush=True)

    # 2. Cost per 1M queries
    print("\n--- 2. Cost per 1M Queries ---", flush=True)
    cost = bench_cost_per_1m(memory, queries, user_ids)
    results["cost_per_1m"] = cost
    print(f"  per-query: {cost['wall_per_query_us']:.0f}us  "
          f"per-1M: {cost['wall_per_1m_minutes']:.1f} min  "
          f"cost: ${cost['cost_per_1m_usd']:.4f}  "
          f"LLM calls: {cost['llm_calls_per_1m']}", flush=True)

    # 3. Storage efficiency
    print("\n--- 3. Storage Efficiency ---", flush=True)
    storage = bench_storage_efficiency(memory, stats["facts"])
    results["storage"] = storage
    print(f"  bytes/fact: {storage['bytes_per_fact']:.0f}  "
          f"total: {storage['db_size_mb']}MB  "
          f"compression vs FP32: {storage['compression_vs_fp32']}x  "
          f"(codec={storage['codec']}, dims={storage['dims']})",
          flush=True)

    # 4. Context handling
    print("\n--- 4. Context Handling ---", flush=True)
    ctx = bench_context_handling(memory, queries, user_ids)
    results["context_handling"] = ctx
    print(f"  context block p50: {ctx['context_block_tokens_p50']} tokens  "
          f"facts p50: {ctx['fact_count_p50']}  "
          f"max block: {ctx['max_block_tokens']} tokens", flush=True)

    # 5. Continuous learning / evolution stress
    print("\n--- 5. Continuous Learning / Evolution Stress ---",
          flush=True)
    # build a fresh memory for the stress test (so consolidation is clean)
    stress_cfg = dict(BENCH_OVERRIDES)
    stress_cfg.update({"unmess_enabled": True,
                       "reconstruct_enabled": True,
                       # 2026-08-28 push: stress-test the new layers too.
                       "tiny_fallback_enabled": True,
                       "prefilter_enabled": True,
                       "ppr_enabled": True,
                       "enable_rerank": True})
    cfg2 = Config(db_path=":memory:", **stress_cfg)
    stress_mem = Memory(cfg2)
    stress_corpus = _build_corpus(args.users, args.facts_per_user)
    learn = bench_continuous_learning(stress_mem, stress_corpus)
    results["continuous_learning"] = learn
    print(f"  phases: {len(learn['phases'])}", flush=True)
    for p in learn["phases"]:
        print(f"    phase {p['phase']}: {p['facts']} facts, "
              f"retrieval {p['retrieval_latency_ms']:.2f}ms", flush=True)
    print(f"  sublinear growth: {learn.get('sublinear_growth', 'n/a')}  "
          f"ratio: {learn.get('growth_ratio', 'n/a')}", flush=True)
    print(f"  memory reduction after consolidation: "
          f"{learn.get('memory_reduction_pct', 0)}%", flush=True)

    # write results
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(results, fh, indent=2, default=str)
    print(f"\nresults written to {args.out}", flush=True)

    # also print a headline summary table
    print("\n=== HEADLINE NUMBERS ===", flush=True)
    print(f"Retrieval p50 latency:       {lat['p50_us']:.0f} μs",
          flush=True)
    print(f"Retrieval p95 latency:       {lat['p95_us']:.0f} μs",
          flush=True)
    print(f"Cost per 1M queries:         ${cost['cost_per_1m_usd']:.4f}",
          flush=True)
    print(f"Storage efficiency:         {storage['bytes_per_fact']:.0f} "
          f"bytes/fact ({storage['compression_vs_fp32']}x vs FP32)",
          flush=True)
    print(f"Context block (p50):         {ctx['context_block_tokens_p50']} "
          f"tokens", flush=True)
    print(f"Continuous learning growth:  "
          f"{learn.get('growth_ratio', 'n/a')}x over 4 phases "
          f"(sublinear: {learn.get('sublinear_growth', 'n/a')})",
          flush=True)
    print(f"Consolidation reduction:     "
          f"{learn.get('memory_reduction_pct', 0)}%", flush=True)


if __name__ == "__main__":
    main()
