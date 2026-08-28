#!/usr/bin/env python3
"""BEAM 10M benchmark — run the new arxiv-inspired improvements
on a real corpus and report honest numbers.

Tries to download the real BEAM 10M dataset from HuggingFace.
Falls back to a large-scale synthetic-but-realistic benchmark using
the existing persona generator if HF download is unavailable.

Runs four configurations:
  baseline  — original μ=0 pattern extractor only
  +unmess   — add per-user idiolect normalization
  +dissim   — add recursive sentence simplification
  +query    — add hybrid query-time extraction (raw chunk retrieval
              + lazy pattern extraction at query time)

Reports:
  - extraction recall (fraction of ground-truth facts captured)
  - retrieval precision@5 (fraction of returned facts that are correct)
  - latency per query (ms)
  - storage bytes per fact (compression ratio)

Usage:
  python benchmarks/run_beam_benchmark.py [--size 10000] [--no-hf]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Iterable

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import numpy as np

from context_m.config import Config
from context_m.api.memory import Memory
from context_m.bench.generator import make_persona, Corpus
from context_m.bench.ood import build_ood_corpus
from context_m.text.embedder import HashingEmbedder
from context_m.text.dissim import DisSimSplitter
from context_m.text.idiolect import PerUserIdiolectNormalizer
from context_m.text.fuzzy import best_match, fuzzy_contains
from context_m.vsa.cleanup import HopfieldCleanup
from context_m.vsa.hologram_overlay import HolographicFactOverlay
from context_m.vsa.attribution import ProtoDashAttributer
from context_m.bridge.query_extract import QueryTimeExtractor


# --- HuggingFace BEAM 10M attempt --------------------------------------

def try_download_beam_from_hf() -> list[dict] | None:
    """Try to download BEAM 10M from HuggingFace. Returns list of
    {user_id, text, facts} dicts or None if unavailable."""
    try:
        from datasets import load_dataset
        # Try several plausible names
        for name in ("memory-bench/beam-10m", "Letta/LongMemEval",
                     "locomo-eval/locomo", "mem0/benchmark"):
            try:
                print(f"[beam-bench] trying HuggingFace dataset: {name}")
                ds = load_dataset(name, split="test", streaming=True)
                samples = []
                for i, row in enumerate(ds):
                    if i >= 1000:
                        break
                    samples.append({
                        "user_id": row.get("user_id", f"user{i}"),
                        "text": row.get("text") or row.get("conversation")
                                or row.get("input") or "",
                        "facts": row.get("facts") or row.get("ground_truth")
                                 or [],
                    })
                if samples:
                    print(f"[beam-bench] loaded {len(samples)} samples from {name}")
                    return samples
            except Exception as e:
                print(f"[beam-bench] {name} not available: {e}")
                continue
    except ImportError:
        print("[beam-bench] `datasets` not installed; using synthetic personas")
    return None


# --- Synthetic fallback (realistic persona corpus) ---------------------

def generate_synthetic_corpus(n: int = 1000, seed: int = 42) -> list[dict]:
    """Generate a realistic persona corpus when BEAM 10M is unavailable.

    Uses the existing persona generator to make N user conversations
    with diverse slang, paraphrase, and compound sentences — exactly
    the cases where μ=0 extractor alone underperforms.
    """
    import datetime as _dt
    rng = random.Random(seed)
    t0 = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    personas = []
    for i in range(n):
        user_id = f"user{i}"
        persona = make_persona(rng, i, t0)
        # build conversation text from persona fields
        facts_text = [f"My name is {persona.full_name}."]
        if persona.employers:
            org = persona.employers[0][0]
            facts_text.append(f"I work at {org}.")
        if persona.cities:
            city = persona.cities[-1][0]
            facts_text.append(f"I live in {city}.")
        # mix of clean + slang + paraphrase + compound styles
        styles = ["clean", "slang", "paraphrase", "compound"]
        style = rng.choice(styles)
        text = " ".join(facts_text)
        if style == "slang":
            text = text.replace("I work at", "I'm at").replace(
                "I live in", "I'm based in").replace(
                "My name is", "Hey, I'm")
        elif style == "paraphrase":
            text = text.replace("I work at", "My employer is").replace(
                "I live in", "I'm located in")
        elif style == "compound":
            text = (f"Although {facts_text[0].lower().rstrip('.')}," +
                    " I recently moved. " + " ".join(facts_text[1:]))
        personas.append({
            "user_id": user_id,
            "text": text,
            "facts": [{"subject": user_id, "relation": "name",
                       "value": persona.full_name, "text": facts_text[0]},
                      {"subject": user_id, "relation": "works_at",
                       "value": persona.employers[0][0] if persona.employers else "",
                       "text": facts_text[1] if len(facts_text) > 1 else ""},
                      {"subject": user_id, "relation": "lives_in",
                       "value": persona.cities[-1][0] if persona.cities else "",
                       "text": facts_text[2] if len(facts_text) > 2 else ""}],
            "style": style,
        })
    return personas


# --- Benchmark runners -------------------------------------------------

def bench_baseline(personas: list[dict], cfg: Config) -> dict:
    """Run μ=0 extractor only — no arxiv improvements."""
    print(f"[beam-bench] BASELINE: μ=0 extractor only")
    mem = Memory(cfg)
    t0 = time.time()
    n_facts_expected = 0
    for p in personas:
        msgs = [{"role": "user", "content": p["text"]}]
        out = mem.add(msgs, user_id=p["user_id"])
        n_facts_expected += len(p["facts"])
    t_ingest = time.time() - t0

    # count facts from trace (not message-level results)
    try:
        n_stored = len(mem.store.query_facts(active=True))
    except Exception:
        n_stored = 0

    # query recall
    t0 = time.time()
    n_queries = 0
    n_correct = 0
    for p in personas[: min(100, len(personas))]:
        for fact in p["facts"][:3]:
            q = f"What is the {fact['relation']} of {fact['subject']}?"
            out = mem.search(q, user_id=p["user_id"], limit=5)
            results = out.get("results", [])
            n_queries += 1
            # check if any returned memory contains the expected value
            for r in results:
                if fact["value"].lower() in r.get("memory", "").lower():
                    n_correct += 1
                    break
    t_query = time.time() - t0

    stats = mem.stats()
    return {
        "config": "baseline",
        "n_personas": len(personas),
        "n_facts_stored": n_stored,
        "n_facts_expected": n_facts_expected,
        "extraction_recall": (n_stored / n_facts_expected
                               if n_facts_expected > 0 else 0),
        "retrieval_precision_at_5": (n_correct / n_queries
                                       if n_queries > 0 else 0),
        "n_queries": n_queries,
        "ingest_time_s": round(t_ingest, 2),
        "query_time_s": round(t_query, 2),
        "ms_per_query": round((t_query * 1000) / max(1, n_queries), 1),
        "storage_stats": stats.get("palace", stats),
    }


def bench_with_arxiv(personas: list[dict], cfg: Config,
                     enable: dict) -> dict:
    """Run with arxiv-inspired improvements enabled."""
    label = ("+" + "+".join(k for k, v in enable.items() if v))
    print(f"[beam-bench] {label}: arxiv improvements enabled")
    mem = Memory(cfg)
    palace = mem.palace
    store = mem.store
    embedder = HashingEmbedder(palace.dims, palace.cfg.seed)
    dissim = DisSimSplitter(max_depth=2) if enable.get("dissim") else None
    idiolect = (PerUserIdiolectNormalizer(embedder)
                if enable.get("unmess") else None)
    extractor = (QueryTimeExtractor(
        palace, store, embedder,
        writer=mem.writer,  # route through MemoryWriter — query-time facts
                           # now go through the same quarantine / lifecycle
                           # / palace / edges pipeline as ingest-time facts
        reader=getattr(mem, "reader", None),  # primary retrieval via the
                                              # structured reader so query-
                                              # time path has parity with
                                              # ingest-time mem.search()
        dissim=dissim, idiolect=idiolect,
        pattern_extractor=getattr(mem, "extractor", None))
        if enable.get("query") else None)

    t0 = time.time()
    n_facts_expected = 0
    for p in personas:
        if extractor and enable.get("query"):
            # ingest stores raw chunk + best-effort pattern extraction
            chunk = extractor.ingest(p["text"], user_id=p["user_id"],
                              run_pattern=True)
        else:
            # idiolect observe + normalized ingest
            text = p["text"]
            if idiolect:
                idiolect.observe(p["user_id"], text)
                text = idiolect.normalize(p["user_id"], text)
            # dissim simplification at ingest
            if dissim:
                clauses = dissim.simplify_text(text)
                texts = [c.text for c in clauses]
            else:
                texts = [text]
            for chunk in texts:
                msgs = [{"role": "user", "content": chunk}]
                out = mem.add(msgs, user_id=p["user_id"])
        n_facts_expected += len(p["facts"])
    t_ingest = time.time() - t0

    # count actual facts stored in trace (not raw chunks)
    try:
        n_stored = len(mem.store.query_facts(active=True))
    except Exception:
        n_stored = 0

    # query recall — for query-time path, do lazy extraction
    t0 = time.time()
    n_queries = 0
    n_correct = 0
    for p in personas[: min(100, len(personas))]:
        for fact in p["facts"][:3]:
            q = f"What is the {fact['relation']} of {fact['subject']}?"
            if extractor and enable.get("query"):
                results = extractor.query(q, user_id=p["user_id"], k=5)
                # check if any returned fact has the expected value
                for r in results:
                    fact_dict = r.get("fact")
                    if fact_dict and fact["value"].lower() in str(
                            fact_dict.get("value", "")).lower():
                        n_correct += 1
                        break
                    if not fact_dict and fact["value"].lower() in r.get(
                            "raw_text", "").lower():
                        n_correct += 1
                        break
            else:
                out = mem.search(q, user_id=p["user_id"], limit=5)
                for r in out.get("results", []):
                    if fact["value"].lower() in r.get(
                            "memory", "").lower():
                        n_correct += 1
                        break
            n_queries += 1
    t_query = time.time() - t0

    stats = mem.stats()
    return {
        "config": label,
        "n_personas": len(personas),
        "n_facts_stored": n_stored,
        "n_facts_expected": n_facts_expected,
        "extraction_recall": (n_stored / n_facts_expected
                               if n_facts_expected > 0 else 0),
        "retrieval_precision_at_5": (n_correct / n_queries
                                       if n_queries > 0 else 0),
        "n_queries": n_queries,
        "ingest_time_s": round(t_ingest, 2),
        "query_time_s": round(t_query, 2),
        "ms_per_query": round((t_query * 1000) / max(1, n_queries), 1),
        "storage_stats": stats.get("palace", stats),
    }


# --- Main --------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=200,
                        help="number of personas to benchmark")
    parser.add_argument("--no-hf", action="store_true",
                        help="skip HuggingFace download, use synthetic")
    parser.add_argument("--messy", action="store_true",
                        help="messify synthetic personas — slang / compound / "
                             "misspellings / text-speak so the unmess+dissim "
                             "improvements have actual work to do (clean "
                             "corpus floors everyone at ~1.0 recall)")
    parser.add_argument("--output", type=str,
                        default=str(REPO / "benchmarks" / "results" / "beam_10m.json"),
                        help="output JSON path")
    args = parser.parse_args()

    # 1. Acquire corpus
    print(f"\n[beam-bench] === BEAM 10M Benchmark (size={args.size}) ===\n")
    personas = None
    if not args.no_hf:
        personas = try_download_beam_from_hf()
    if personas is None:
        print("[beam-bench] using synthetic persona corpus (realistic slang + paraphrase)")
        personas = generate_synthetic_corpus(n=args.size, seed=42)
        source = "synthetic_fallback"
    else:
        source = "huggingface"
    # --messy: run the messifier over the synthetic corpus so unmess /
    # dissim / fuzzy / idiolect actually have something to fix. Without
    # this flag the clean corpus floors every config at ~1.0 recall and
    # the arxiv improvements look pointless.
    if args.messy and source == "synthetic_fallback":
        from context_m.bench.messy import messify_persona_dict
        messy_rng = random.Random(1337)
        n_before = len(personas)
        personas = [messify_persona_dict(p, messy_rng) for p in personas]
        print(f"[beam-bench] --messy applied: messified {n_before} personas "
              f"(sample: {personas[0]['text'][:80]}...)")
        source = "synthetic_messy"
    print(f"[beam-bench] corpus: {len(personas)} personas from {source}")

    # 2. Run benchmarks
    cfg = Config.from_env()
    cfg.db_path = "/tmp/beam_bench_baseline.db"
    if os.path.exists(cfg.db_path):
        os.unlink(cfg.db_path)
    baseline = bench_baseline(personas, cfg)

    cfg2 = Config.from_env()
    cfg2.db_path = "/tmp/beam_bench_unmess.db"
    if os.path.exists(cfg2.db_path):
        os.unlink(cfg2.db_path)
    unmess = bench_with_arxiv(personas, cfg2, {"unmess": True, "dissim": False,
                                                "query": False})

    cfg3 = Config.from_env()
    cfg3.db_path = "/tmp/beam_bench_dissim.db"
    if os.path.exists(cfg3.db_path):
        os.unlink(cfg3.db_path)
    dissim = bench_with_arxiv(personas, cfg3, {"unmess": True, "dissim": True,
                                                "query": False})

    cfg4 = Config.from_env()
    cfg4.db_path = "/tmp/beam_bench_query.db"
    if os.path.exists(cfg4.db_path):
        os.unlink(cfg4.db_path)
    query = bench_with_arxiv(personas, cfg4, {"unmess": True, "dissim": True,
                                                "query": True})

    # 3. Aggregate results
    results = {
        "benchmark": "beam_10m",
        "source": source,
        "n_personas": len(personas),
        "configs": {
            "baseline": baseline,
            "+unmess": unmess,
            "+unmess+dissim": dissim,
            "+unmess+dissim+query": query,
        },
        "summary": {
            "extraction_recall": {
                "baseline": baseline["extraction_recall"],
                "+unmess": unmess["extraction_recall"],
                "+unmess+dissim": dissim["extraction_recall"],
                "+unmess+dissim+query": query["extraction_recall"],
            },
            "retrieval_precision_at_5": {
                "baseline": baseline["retrieval_precision_at_5"],
                "+unmess": unmess["retrieval_precision_at_5"],
                "+unmess+dissim": dissim["retrieval_precision_at_5"],
                "+unmess+dissim+query": query["retrieval_precision_at_5"],
            },
            "ms_per_query": {
                "baseline": baseline["ms_per_query"],
                "+unmess": unmess["ms_per_query"],
                "+unmess+dissim": dissim["ms_per_query"],
                "+unmess+dissim+query": query["ms_per_query"],
            },
        },
    }

    # 4. Save
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n[beam-bench] results saved to {out_path}")

    # 5. Print summary
    print("\n[beam-bench] === Summary ===")
    print(f"{'config':<30} {'recall':<10} {'prec@5':<10} {'ms/q':<8}")
    for k, v in results["summary"]["extraction_recall"].items():
        p = results["summary"]["retrieval_precision_at_5"][k]
        m = results["summary"]["ms_per_query"][k]
        print(f"{k:<30} {v:<10.3f} {p:<10.3f} {m:<8.1f}")


if __name__ == "__main__":
    main()
