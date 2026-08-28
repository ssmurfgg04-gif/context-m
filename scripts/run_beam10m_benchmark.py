#!/usr/bin/env python3
"""Real BEAM-10M benchmark — runs Context-M's pipeline on actual data.

Loads N conversations from Mohammadta/BEAM-10M, ingests the user turns
into Context-M (zero LLM calls — deterministic μ=0 extractor), then
queries for the ground-truth user_profile facts and reports recall.

Reports:
  - extraction_recall: fraction of ground-truth facts found by the
    μ=0 extractor (no LLM in the loop)
  - retrieval_precision@5: fraction of returned memories that contain
    the expected value
  - latency per ingest (ms)
  - latency per query (ms)
  - storage bytes per fact

The honest gap: BEAM-10M conversations are LONG (16KB of chat per
persona) with facts scattered across hundreds of turns. The μ=0
pattern extractor finds some facts directly ("My name is X") and
misses others (facts implied across multiple turns or buried in
narrative context). An LLM extractor would do better — at 1000x the
cost. We report both numbers honestly.

Usage:
    python scripts/run_beam10m_benchmark.py [--n-personas 2] [--max-turns 50]
"""
from __future__ import annotations

# --- determinism lockdown ----------------------------------------------
# BEAM-10M bench variance was 43-49% across identical runs because:
#   (a) PYTHONHASHSEED randomizes set/dict iteration order per process,
#       so any tie-break on id (or set-keyed candidate_ids iteration)
#       shuffles results across runs
#   (b) OpenBLAS uses non-deterministic summation order for matmul,
#       so `qv @ centroid` differs at the ULP level across processes,
#       and argsort breaks the ULP-level ties differently
# Force them both OFF before importing anything that touches numpy/dicts.
import os
os.environ.setdefault("PYTHONHASHSEED", "0")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from context_m.api.memory import Memory
from context_m.config import Config
from context_m.bench.beam_loader import (
    load_beam_rows, beam_rows_to_personas)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-personas", type=int, default=2,
                    help="number of BEAM-10M conversations to load")
    ap.add_argument("--max-turns", type=int, default=50,
                    help="max user turns to ingest per persona")
    ap.add_argument("--config", default="baseline",
                    choices=["baseline", "+unmess", "+unmess+dissim",
                             "+unmess+dissim+query", "+unmess+dissim+rerank",
                             "+full_v3", "+all_v2", "all"],
                    help="which feature stack to enable; 'all' runs the 4 "
                         "original configs, '+all_v2' adds the rerank stack, "
                         "'+full_v3' adds the 2026-08-28 push (tiny_fallback "
                         "+ prefilter + ppr + rerank + unmess + dissim)")
    ap.add_argument("--cache-dir", default="/tmp/beam_cache",
                    help="where to cache BEAM rows (avoid re-downloading)")
    ap.add_argument("--db", default="/tmp/beam10m_bench.db",
                    help="path for the bench DB")
    ap.add_argument("--out", default=str(REPO / "benchmarks" / "results"
                                          / "beam10m_real.json"),
                    help="output JSON path")
    args = ap.parse_args()

    # Re-exec ourselves with PYTHONHASHSEED=0 if the parent didn't set it.
    # PYTHONHASHSEED is read at interpreter startup, so setting it from
    # inside Python has no effect on the running process — but re-exec'ing
    # with a fresh env DOES, and is a no-op if already set.
    if os.environ.get("PYTHONHASHSEED", "") != "0":
        print("[beam10m-bench] PYTHONHASHSEED not set; re-exec'ing with seed=0 "
              "for stable prec@5 numbers...")
        os.environ["PYTHONHASHSEED"] = "0"
        os.execve(sys.executable, [sys.executable, *sys.argv], os.environ)

    print(f"\n[beam10m-bench] === Real BEAM-10M Benchmark ===")
    print(f"[beam10m-bench] PYTHONHASHSEED={os.environ.get('PYTHONHASHSEED')}")
    print(f"[beam10m-bench] personas: {args.n_personas}")
    print(f"[beam10m-bench] max turns/persona: {args.max_turns}")
    print(f"[beam10m-bench] config: {args.config}\n")

    # 1. Load BEAM rows
    print(f"[beam10m-bench] loading BEAM-10M rows (cache: {args.cache_dir})...")
    rows = load_beam_rows(n=args.n_personas, cache_dir=args.cache_dir)
    personas = beam_rows_to_personas(rows, max_turns_per_persona=args.max_turns)
    total_facts = sum(len(p["facts"]) for p in personas)
    total_chars = sum(len(p["text"]) for p in personas)
    print(f"[beam10m-bench] loaded {len(personas)} personas, "
          f"{total_facts} ground-truth facts, {total_chars:,} chars total")
    for p in personas:
        print(f"  - {p['user_id']}: {p['n_turns']} turns, "
              f"{len(p['facts'])} facts, {len(p['text']):,} chars")

    # decide which configs to run
    if args.config == "all":
        configs_to_run = ["baseline", "+unmess", "+unmess+dissim",
                           "+unmess+dissim+query"]
    elif args.config == "+all_v2":
        # the new SOTA-inspired stack: full feature set + cross-encoder rerank
        configs_to_run = ["baseline", "+unmess+dissim",
                           "+unmess+dissim+rerank"]
    elif args.config == "+full_v3":
        # 2026-08-28 push: baseline + full feature stack with new
        # tiny_fallback + prefilter layers (the user's "--rerank --ppr"
        # request). PPR is on by default in Config.
        configs_to_run = ["baseline", "+unmess+dissim+rerank", "+full_v3"]
    else:
        configs_to_run = [args.config]

    all_results = {}
    for config_name in configs_to_run:
        print(f"\n[beam10m-bench] --- running config: {config_name} ---")
        result = run_single_config(personas, config_name, args)
        all_results[config_name] = result

    # final summary
    if len(all_results) > 1:
        print(f"\n[beam10m-bench] === ALL CONFIGS SUMMARY ===")
        print(f"  {'config':<32s} {'extract_recall':>15s} {'retriev_prec@5':>15s} "
              f"{'ms/q':>6s}")
        for cn, r in all_results.items():
            print(f"  {cn:<32s} {r['extraction_recall']:>15.4f} "
                  f"{r['retrieval_precision_at_5']:>15.4f} "
                  f"{r['ms_per_query']:>6.1f}")

    # save combined report
    out = {
        "benchmark": "beam10m_real",
        "source": "Mohammadta/BEAM-10M",
        "n_personas": len(personas),
        "max_turns_per_persona": args.max_turns,
        "total_chars": total_chars,
        "total_ground_truth_facts": total_facts,
        "configs": all_results,
        "personas": [{
            "user_id": p["user_id"],
            "n_turns": p["n_turns"],
            "n_facts": len(p["facts"]),
            "text_chars": len(p["text"]),
            "facts": p["facts"],
        } for p in personas],
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"\n[beam10m-bench] report saved to {out_path}")
    return 0


def run_single_config(personas, config_name, args):
    """Run a single config and return its result dict."""
    if os.path.exists(args.db):
        os.unlink(args.db)
    cfg = Config.from_env()
    cfg.db_path = args.db
    # NEW: enable cross-encoder rerank for configs that include "+rerank"
    if "rerank" in config_name:
        cfg.enable_rerank = True
    # 2026-08-28 push: enable the new tiny_fallback + prefilter layers
    # for the +full_v3 config. They're on by default in production but
    # the bench baselines leave them off; we opt them in here.
    if "full_v3" in config_name:
        cfg.tiny_fallback_enabled = True
        cfg.prefilter_enabled = True
        cfg.enable_rerank = True
        cfg.unmess_enabled = True  # already triggered by "unmess" substring below
        cfg.bitap_trigger_enabled = True
        # PPR is on by default; explicit for clarity
        cfg.ppr_enabled = True
    # Bench determinism: SLB bypassed so templated near-duplicate queries
    # (cosine ≈ 0.97 against the threshold) don't flip hit/miss on BLAS
    # ULP drift. Every query recomputes fresh fusion — the bench measures
    # the FUSION quality, not SLB cache locality.
    cfg.slb_disabled = True
    mem = Memory(cfg)

    # optional feature stacks
    idiolect = None
    dissim = None
    # 2026-08-28 push: +full_v3 must also run the unmess + dissim
    # preprocessing path (idiolect normalization + DisSim compound
    # sentence splitter), because those operate at the BENCH SCRIPT
    # level — they rewrite the input text BEFORE mem.add(). The Config
    # flags cfg.unmess_enabled etc. are for the extractor's internal
    # path, which is different.
    use_unmess = "unmess" in config_name or "full_v3" in config_name
    use_dissim = "dissim" in config_name or "full_v3" in config_name
    if use_unmess:
        from context_m.text.embedder import HashingEmbedder
        from context_m.text.idiolect import PerUserIdiolectNormalizer
        idiolect = PerUserIdiolectNormalizer(
            HashingEmbedder(mem.palace.dims, mem.palace.cfg.seed))
    if use_dissim:
        from context_m.text.dissim import DisSimSplitter
        dissim = DisSimSplitter(max_depth=2)

    print(f"[beam10m-bench] ingesting {len(personas)} personas...")
    t0 = time.time()
    for p in personas:
        text = p["text"]
        if idiolect:
            idiolect.observe(p["user_id"], text)
            text = idiolect.normalize(p["user_id"], text)
        if dissim:
            clauses = dissim.simplify_text(text)
            texts = [c.text for c in clauses]
        else:
            texts = [text]
        for chunk in texts:
            mem.add([{"role": "user", "content": chunk}],
                    user_id=p["user_id"])
    t_ingest = time.time() - t0
    n_facts_stored = len(mem.store.query_facts(active=True))
    print(f"[beam10m-bench] ingested in {t_ingest:.2f}s — "
          f"{n_facts_stored} facts extracted (μ=0, no LLM)")

    # query each ground-truth fact and check recall
    print(f"[beam10m-bench] querying ground-truth facts...")
    n_queries = 0
    n_correct_extract = 0
    n_correct_retrieve = 0
    t0 = time.time()
    for p in personas:
        for fact in p["facts"]:
            q = f"What is the {fact['relation']} of {p['user_id']}?"
            out = mem.search(q, user_id=p["user_id"], limit=5)
            results = out.get("results", [])
            n_queries += 1
            for r in results:
                if fact["value"].lower() in r.get("memory", "").lower():
                    n_correct_retrieve += 1
                    break
        all_stored = mem.store.query_facts(user_id=p["user_id"], active=True)
        stored_values = {f.value.lower() for f in all_stored}
        for fact in p["facts"]:
            if fact["value"].lower() in stored_values:
                n_correct_extract += 1
    t_query = time.time() - t0

    total_facts = sum(len(p["facts"]) for p in personas)
    extraction_recall = (n_correct_extract / total_facts
                          if total_facts > 0 else 0)
    retrieval_precision = (n_correct_retrieve / n_queries
                              if n_queries > 0 else 0)

    stats = mem.stats()
    storage_bytes_per_fact = 0
    if n_facts_stored > 0:
        sp = stats.get("palace", stats)
        if isinstance(sp, dict):
            storage_bytes_per_fact = sp.get("bytes_per_memory", 0)

    result = {
        "config": config_name,
        "n_facts_stored": n_facts_stored,
        "extraction_recall": round(extraction_recall, 4),
        "retrieval_precision_at_5": round(retrieval_precision, 4),
        "ingest_time_s": round(t_ingest, 2),
        "query_time_s": round(t_query, 2),
        "ms_per_ingest_persona": round((t_ingest * 1000)
                                          / max(len(personas), 1), 1),
        "ms_per_query": round((t_query * 1000) / max(n_queries, 1), 1),
        "storage_bytes_per_fact": storage_bytes_per_fact,
        "llm_calls": 0,
    }
    print(f"[beam10m-bench] {config_name}: extract_recall={extraction_recall:.4f}, "
          f"retriev_prec@5={retrieval_precision:.4f}, "
          f"ms/query={(t_query*1000)/max(n_queries,1):.1f}")
    mem.close()
    if os.path.exists(args.db):
        os.unlink(args.db)
    return result


if __name__ == "__main__":
    sys.exit(main())
