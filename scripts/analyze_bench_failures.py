#!/usr/bin/env python3
"""Failure analysis: which ground-truth facts does the rerank config miss?

For each persona, runs +unmess+dissim+rerank, then for each ground-truth
fact checks if the value appears in the top-5 results. Prints misses so we
can see what kinds of facts the rerank doesn't recover.

Usage:
    python scripts/analyze_bench_failures.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from cortexm.api.memory import Memory
from cortexm.config import Config
from cortexm.bench.beam_loader import (
    load_beam_rows, beam_rows_to_personas)
from cortexm.text.embedder import HashingEmbedder
from cortexm.text.idiolect import PerUserIdiolectNormalizer
from cortexm.text.dissim import DisSimSplitter


def main():
    print("[analyze] loading BEAM-10M rows...")
    rows = load_beam_rows(n=10, cache_dir="/tmp/beam_cache")
    personas = beam_rows_to_personas(rows, max_turns_per_persona=50)
    print(f"[analyze] loaded {len(personas)} personas, "
          f"{sum(len(p['facts']) for p in personas)} facts total")

    cfg = Config.from_env()
    cfg.db_path = "/tmp/analyze_bench.db"
    cfg.enable_rerank = True
    if os.path.exists(cfg.db_path):
        os.unlink(cfg.db_path)
    mem = Memory(cfg)
    idiolect = PerUserIdiolectNormalizer(
        HashingEmbedder(mem.palace.dims, mem.palace.cfg.seed))
    dissim = DisSimSplitter(max_depth=2)

    print("[analyze] ingesting personas with +unmess+dissim...")
    t0 = time.time()
    for p in personas:
        text = p["text"]
        idiolect.observe(p["user_id"], text)
        text = idiolect.normalize(p["user_id"], text)
        for clause in dissim.simplify_text(text):
            mem.add([{"role": "user", "content": clause.text}],
                    user_id=p["user_id"])
    print(f"[analyze] ingested in {time.time()-t0:.1f}s")

    hits = 0
    misses = 0
    miss_list = []
    for p in personas:
        for fact in p["facts"]:
            q = f"What is the {fact['relation']} of {p['user_id']}?"
            out = mem.search(q, user_id=p["user_id"], limit=5)
            results = out.get("results", [])
            found = any(fact["value"].lower() in r.get("memory", "").lower()
                        for r in results)
            if found:
                hits += 1
            else:
                misses += 1
                miss_list.append({
                    "persona": p["user_id"],
                    "fact": fact,
                    "query": q,
                    "top5": [r["memory"] for r in results],
                    "intent": out.get("intent"),
                    "rerank": out.get("timing", {}).get("rerank"),
                })

    total = hits + misses
    print(f"\n[analyze] === HITS={hits}/{total} ({hits/total*100:.1f}%), "
          f"misses={misses}/{total} ({misses/total*100:.1f}%) ===")
    print(f"\n[analyze] === {len(miss_list)} MISSES ===")
    for m in miss_list:
        print(f"\n  persona: {m['persona']}")
        print(f"  fact:    {m['fact']}")
        print(f"  query:   {m['query']}")
        print(f"  intent:  {m['intent']}, rerank={m['rerank']}")
        print(f"  top5:")
        for i, r in enumerate(m['top5']):
            print(f"    {i+1}. {r}")

    mem.close()
    if os.path.exists(cfg.db_path):
        os.unlink(cfg.db_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
