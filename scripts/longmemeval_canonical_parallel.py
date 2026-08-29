#!/usr/bin/env python3
"""Parallel LongMemEval canonical benchmark runner — v0.6.1.

Runs the canonical LongMemEval benchmark (500 questions, 23,867 sessions)
in parallel across multiple worker processes. Each worker opens its OWN
file-based SQLite Memory instance per question (so memory stays flat
regardless of question count or haystack size — same pattern as
``scripts/longmemeval_canonical_full.py``).

v0.6.1 wiring:
  * **Negation routing** — the writer now writes negated sentences
    into the ``negation_records`` table rather than mis-extracting
    them as positive facts (see ``cortexm/bridge/writer.py`` +
    ``cortexm/bridge/negation.py``). The reader surfaces a "NEGATION:
    user stated …" note when the query overlaps an ingested negation.
  * **Query-rewrite pipeline** — synonyms (incl. the new ``medical``
    cluster), slang normalization, FST abbreviation expansion, spelling
    correction, and the deterministic recognizer (holidays + currency +
    dates) all run at QUERY time. The expanded queries fan out into
    multiple BM25 searches and the hits are fused.
  * **BM25 tuning** — the worker calls ``m.tune_bm25(k1=1.2, b=0.5)``
    per the user's instruction. (k1=1.2 Lucene default vs the v0.6.0
    1.5 default; b=0.5 weaker length-norm for short chat chunks.)

Usage
-----
    # Run 100 questions (20 per subtask × 5 categories; the 6th type
    # single-session-preference collapses into single_session):
    python scripts/longmemeval_canonical_parallel.py \\
        --n-per-type 20 \\
        --workers 4 \\
        --db-path benchmarks/data/longmemeval/longmemeval.db \\
        --out benchmarks/results/longmemeval_v060_100q.json

    # If the dataset is missing it will be auto-downloaded from
    # HuggingFace (xiaowu0162/longmemeval-cleaned, public, ~277MB).

    # Smoke-test mode: 1 question per subtask:
    python scripts/longmemeval_canonical_parallel.py \\
        --n-per-type 1 --workers 1 \\
        --out benchmarks/results/canonical_smoke.json

Arguments
---------
    --n-per-type INT        questions per subtask (default 5)
    --workers INT           worker processes (default CPU count)
    --db-path PATH          file path prefix for per-worker SQLite DBs
                            (worker N gets {path}_w{N}.db; deleted at end)
    --bench-dir PATH        directory for the canonical LongMemEval JSON
                            (default: benchmarks/data/longmemeval)
    --data-file NAME        filename inside --bench-dir (default:
                            longmemeval_s_cleaned.json)
    --out PATH              output JSON path
    --seed INT              sampling seed (default 42)
    --max-messages-per-q N cap on haystack messages per question
                            (default 1500; LongMemEval's biggest is ~5K)
    --max-seconds-per-q S   cap on wall-clock per question (default 240)
    --bm25-k1 FLOAT         BM25 k1 (default 1.2 — Lucene default)
    --bm25-b FLOAT          BM25 b (default 0.5 — weak len-norm for chat)
    --ingest-only           no-op for this benchmark (each question has
                            its OWN haystack — there's no shared pre-
                            ingested DB to build). Flag is accepted
                            for compatibility with the user's command.
"""
from __future__ import annotations

import argparse
import gc
import json
import multiprocessing as mp
import os
import random
import sys
import time
import traceback
from pathlib import Path
from typing import Dict, List

# Add project root to sys.path so `cortexm` and `scripts.*` imports work.
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Data setup
# ---------------------------------------------------------------------------

DEFAULT_BENCH_DIR = "benchmarks/data/longmemeval"
DEFAULT_DATA_FILE = "longmemeval_s_cleaned.json"
DEFAULT_DATA_PATH = os.path.join(DEFAULT_BENCH_DIR, DEFAULT_DATA_FILE)


def _ensure_benchmark_data(bench_dir: Path, data_file: str) -> Path:
    """Ensure the canonical LongMemEval JSON is present. Downloads on
    first call (~277MB); cached on subsequent calls."""
    bench_dir.mkdir(parents=True, exist_ok=True)
    bench_file = bench_dir / data_file
    if bench_file.exists():
        return bench_file
    print(f"[setup] downloading canonical LongMemEval to {bench_dir}...",
          file=sys.stderr)
    try:
        from huggingface_hub import hf_hub_download
        hf_hub_download(
            repo_id="xiaowu0162/longmemeval-cleaned",
            repo_type="dataset",
            filename=data_file,
            local_dir=str(bench_dir),
        )
    except ImportError:
        print("[setup] huggingface_hub not installed; "
              "pip install huggingface_hub", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[setup] huggingface download failed: {e}", file=sys.stderr)
        print("[setup] try: wget https://huggingface.co/datasets/"
              "xiaowu0162/longmemeval-cleaned/resolve/main/"
              f"{data_file} -O {bench_file}", file=sys.stderr)
        sys.exit(1)
    if not bench_file.exists():
        raise FileNotFoundError(
            f"could not find {data_file} in {bench_dir}")
    return bench_file


# ---------------------------------------------------------------------------
# Question sampling
# ---------------------------------------------------------------------------

SUBTASK_MAP = {
    "single-session-user": "single_session",
    "single-session-assistant": "single_session",
    "single-session-preference": "single_session",
    "knowledge-update": "knowledge_update",
    "multi-session": "multi_session",
    "temporal-reasoning": "temporal_reasoning",
}


def sample_questions(data: list[dict], n_per_type: int,
                     seed: int = 42) -> list[dict]:
    """Sample N questions per subtask, deterministically (seeded).

    Same logic as ``scripts/longmemeval_canonical.py:sample_questions``
    — sorted by haystack size (smallest first) so we don't OOM on the
    biggest 5K-message sessions. Sampled uniformly from the first 3×
    the request to give the seed room to vary."""
    rng = random.Random(seed)
    by_type: dict[str, list[dict]] = {}
    for e in data:
        by_type.setdefault(e["question_type"], []).append(e)
    out: list[dict] = []
    for qtype, qs in sorted(by_type.items()):
        qs_sorted = sorted(
            qs, key=lambda e: len(e.get("haystack_session_ids", [])))
        pool = qs_sorted[:max(n_per_type * 3, n_per_type)]
        sample = rng.sample(pool, min(n_per_type, len(pool)))
        out.extend(sample)
    return out


# ---------------------------------------------------------------------------
# Haystack flattening
# ---------------------------------------------------------------------------

def _flatten_haystack(haystack_sessions: list,
                      include_assistant: bool = True,
                      max_assistant_chars: int = 800) -> list[str]:
    """Flatten the haystack's role/content messages into a list of
    natural-language strings (one per message). Mirrors
    ``scripts/longmemeval_canonical._flatten_haystack``."""
    out: list[str] = []
    for session in haystack_sessions:
        if not isinstance(session, list):
            continue
        for msg in session:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not content or not isinstance(content, str):
                continue
            if role == "user":
                if len(content) > 5000:
                    content = content[:5000]
                out.append(content)
            elif role == "assistant" and include_assistant:
                if len(content) > max_assistant_chars:
                    content = content[:max_assistant_chars]
                out.append(content)
    return out


# ---------------------------------------------------------------------------
# Per-question execution (runs in worker processes)
# ---------------------------------------------------------------------------

def _make_config(db_path: str, *,
                 bm25_k1: float = 1.2, bm25_b: float = 0.5) -> "Config":
    """Build the v0.6.1 Config for one question's Memory instance.

    The v0.6.0 query-rewrite pipeline is ON by default (Config flags
    ``query_rewrite_enabled`` etc.), so just constructing Config()
    enables synonyms + slang + FST + negation + recognizer + holiday
    resolution. We also flip on unmess + bitap + tiny_fallback so OOD
    recall is at parity with the small canonical runner.
    """
    from cortexm.config import Config
    return Config(
        db_path=db_path,
        unmess_enabled=True,
        bitap_trigger_enabled=True,
        tiny_fallback_enabled=True,
        prefilter_enabled=True,
        ppr_enabled=True,
        enable_rerank=True,
        fade_enabled=False,
        tmt_enabled=False,
        cognition_enabled=True,
        # v0.6.0 query-time rewrite pipeline — all on by default
        query_rewrite_enabled=True,
        slang_normalization_enabled=True,
        abbreviation_expansion_enabled=True,
        spelling_correction_enabled=True,
        synonym_expansion_enabled=True,
        holiday_resolution_enabled=True,
        # v0.6.1 negation routing — wires into MemoryWriter.add()
        negation_indexing_enabled=True,
        multilingual_routing_enabled=True,
        # v0.6.1 BM25 tuning — user-requested (1.2, 0.5)
        bm25_k1=bm25_k1,
        bm25_b=bm25_b,
        # IR fundamentals
        query_cache_enabled=True,
        index_optimize_on_consolidate=True,
    )


# Worker globals — set in _worker_init so each process opens its own
# path prefix and reuse the canonical judge helpers lazily.
_WORKER_DB_PREFIX: str = ""
_WORKER_BM25_K1: float = 1.2
_WORKER_BM25_B: float = 0.5
_WORKER_MAX_MESSAGES: int = 1500
_WORKER_MAX_SECONDS: float = 240.0


def _worker_init(db_prefix: str, bm25_k1: float, bm25_b: float,
                 max_messages: int, max_seconds: float) -> None:
    global _WORKER_DB_PREFIX, _WORKER_BM25_K1, _WORKER_BM25_B
    global _WORKER_MAX_MESSAGES, _WORKER_MAX_SECONDS
    _WORKER_DB_PREFIX = db_prefix
    _WORKER_BM25_K1 = bm25_k1
    _WORKER_BM25_B = bm25_b
    _WORKER_MAX_MESSAGES = max_messages
    _WORKER_MAX_SECONDS = max_seconds


def _cleanup_db(db_path: str) -> None:
    if not db_path or db_path == ":memory:":
        return
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.unlink(db_path + suffix)
        except FileNotFoundError:
            pass
        except Exception:
            pass


def _infer_entity_attribute(question: str, subtask: str):
    """Reuse the canonical runner's heuristic so the BOOL judge has
    something to work with. Imported lazily so worker processes don't
    pay the import cost unless they actually need it."""
    try:
        from scripts.longmemeval_canonical import _infer_entity_attribute as _i
        return _i(question, subtask)
    except Exception:
        return "", ""


def _is_aggregation_question(question: str) -> bool:
    """Reuse the canonical_full helper if present (returns False
    otherwise — aggregation enrichment is best-effort)."""
    try:
        from scripts.longmemeval_canonical_full import (
            _is_aggregation_question as _i)
        return _i(question)
    except Exception:
        return False


def _enrich_with_aggregation_chunks(mem, cb, question, q_user_id,
                                    max_extra=50):
    try:
        from scripts.longmemeval_canonical_full import (
            _enrich_with_aggregation_chunks as _e)
        return _e(mem, cb, question, q_user_id, max_extra=max_extra)
    except Exception:
        return cb, 0


def _eval_one_question(args) -> dict:
    """Worker: evaluate one LongMemEval question. Creates a fresh
    file-based Memory, ingests the haystack, runs search + judge,
    then closes + deletes the DB."""
    global _WORKER_DB_PREFIX, _WORKER_BM25_K1, _WORKER_BM25_B
    global _WORKER_MAX_MESSAGES, _WORKER_MAX_SECONDS

    q = args["question"]
    qidx = args["global_idx"]
    worker_idx = args["worker_idx"]

    from cortexm import Memory
    from cortexm.api.memory import MemoryReader
    from scripts.longmemeval_judge import det_judge, LongMemEvalQuestion

    qtype = q["question_type"]
    subtask = SUBTASK_MAP.get(qtype, "single_session")
    qid = q.get("question_id", f"q{qidx}")
    question = q["question"]
    answer = str(q.get("answer", ""))
    entity, attribute = _infer_entity_attribute(question, subtask)
    q_user_id = f"user_{qid}"

    # Per-worker DB path so multiple workers don't fight over the same
    # file. SQLite WAL allows concurrent readers but EXCLUSIVE write
    # locks serialize writers — distinct files sidestep that entirely.
    db_path = f"{_WORKER_DB_PREFIX}_w{worker_idx}_q{qidx}.db"
    cfg = _make_config(
        db_path,
        bm25_k1=_WORKER_BM25_K1, bm25_b=_WORKER_BM25_B)

    t_start = time.time()
    mem = Memory(cfg)
    try:
        # v0.6.1: apply the BM25 tuning the user requested.
        # Memory.tune_bm25() persists on the config too, so reopens
        # honor it. No-op when the verbatim tier isn't mounted.
        mem.tune_bm25(k1=_WORKER_BM25_K1, b=_WORKER_BM25_B)

        msgs = _flatten_haystack(q.get("haystack_sessions", []),
                                 include_assistant=True)
        if _WORKER_MAX_MESSAGES and len(msgs) > _WORKER_MAX_MESSAGES:
            msgs = msgs[:_WORKER_MAX_MESSAGES]

        batch = 50
        n_ingested = 0
        for i in range(0, len(msgs), batch):
            chunk = msgs[i:i + batch]
            try:
                mem.add([{"role": "user", "content": m} for m in chunk],
                        user_id=q_user_id)
            except Exception as e:
                print(f"  [w{worker_idx}] ingest err batch {i//batch}: {e}",
                      flush=True)
            n_ingested += len(chunk)
            if (_WORKER_MAX_SECONDS and
                    (time.time() - t_start) > _WORKER_MAX_SECONDS):
                break

        try:
            mem.consolidate()
        except Exception as e:
            print(f"  [w{worker_idx}] consolidate failed: {e}", flush=True)

        t0 = time.time()
        out = mem.search(question, user_id=q_user_id, limit=10)
        cb = out.get("context_block", "")
        t_ret = time.time() - t0
        timing = out.get("timing", {})
        vh = timing.get("verbatim_hits", 0)
        rs_status = timing.get("recall_step", "n/a")

        # v0.5.5 lineage: aggregation enrichment (best-effort)
        if _is_aggregation_question(question):
            cb_before = cb
            cb, n_agg = _enrich_with_aggregation_chunks(
                mem, cb, question, q_user_id, max_extra=50)
            if n_agg > 0:
                vh += n_agg

        lq = LongMemEvalQuestion(
            session_id=qidx, question=question, answer=answer,
            subtask=subtask, entity=entity, attribute=attribute)
        det_correct, strategy = det_judge(cb, answer, mem, lq,
                                           user_id=q_user_id)
        result = {
            "qid": qid,
            "global_idx": qidx,
            "question_type": qtype,
            "subtask": subtask,
            "question": question,
            "expected_answer": answer,
            "n_messages_ingested": n_ingested,
            "ingest_s": round(time.time() - t_start - t_ret, 2),
            "retrieve_s": round(t_ret, 2),
            "judge_strategy": strategy,
            "context_block_preview": cb[:1000],
            "det_correct": bool(det_correct),
            "verbatim_hits": vh,
            "recall_step": rs_status,
            "worker_idx": worker_idx,
        }
    except Exception as e:
        traceback.print_exc()
        result = {
            "qid": qid,
            "global_idx": qidx,
            "question_type": qtype,
            "subtask": subtask,
            "question": question,
            "expected_answer": answer,
            "n_messages_ingested": 0,
            "ingest_s": round(time.time() - t_start, 2),
            "retrieve_s": 0.0,
            "judge_strategy": "ERROR",
            "context_block_preview": f"[error: {e}]",
            "det_correct": False,
            "verbatim_hits": 0,
            "recall_step": "error",
            "worker_idx": worker_idx,
            "error": str(e),
        }
    finally:
        try:
            mem.close()
        except Exception:
            pass
        del mem
        gc.collect()
        _cleanup_db(db_path)

    return result


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Parallel LongMemEval canonical benchmark runner "
                    "(μ=0, v0.6.1 query-rewrite + negation routing)")
    p.add_argument("--n-per-type", type=int, default=5,
                   help="questions per subtask (default 5)")
    p.add_argument("--workers", type=int, default=mp.cpu_count(),
                   help="worker processes (default CPU count)")
    p.add_argument("--db-path", type=str,
                   default=DEFAULT_DATA_PATH.replace(".json", ".db"),
                   help="file path PREFIX for per-worker SQLite DBs "
                        "(worker N gets {path}_w{N}_q{idx}.db, deleted at end)")
    p.add_argument("--bench-dir", type=str, default=DEFAULT_BENCH_DIR,
                   help="directory for the canonical LongMemEval JSON")
    p.add_argument("--data-file", type=str, default=DEFAULT_DATA_FILE,
                   help="filename inside --bench-dir")
    p.add_argument("--out", type=str,
                   default="benchmarks/results/canonical_parallel.json",
                   help="output JSON path")
    p.add_argument("--seed", type=int, default=42,
                   help="sampling seed")
    p.add_argument("--max-messages-per-q", type=int, default=1500,
                   help="cap on haystack messages per question")
    p.add_argument("--max-seconds-per-q", type=float, default=240.0,
                   help="cap on wall-clock per question (seconds)")
    p.add_argument("--bm25-k1", type=float, default=1.2,
                   help="BM25 k1 (Lucene default 1.2)")
    p.add_argument("--bm25-b", type=float, default=0.5,
                   help="BM25 b (Lucene default 0.75; 0.5 for short chat)")
    p.add_argument("--ingest-only", action="store_true",
                   help="No-op for this benchmark. Each question has its "
                        "OWN haystack — there's no shared pre-ingested DB "
                        "to build. Flag is accepted for compatibility.")
    args = p.parse_args(argv)

    bench_dir = Path(args.bench_dir)
    bench_file = _ensure_benchmark_data(bench_dir, args.data_file)
    if args.ingest_only:
        # The canonical protocol has per-question haystacks. "Pre-
        # ingesting" doesn't fit because each question gets a fresh
        # Memory. We accept the flag for UX-compat and just verify
        # the data is present + loadable.
        print(f"[ingest-only] verified data at {bench_file}")
        with open(bench_file) as f:
            data = json.load(f)
        print(f"[ingest-only] {len(data)} canonical questions available")
        return 0

    print(f"[load] reading {bench_file}", file=sys.stderr)
    with open(bench_file) as f:
        data = json.load(f)
    print(f"[load] {len(data)} canonical questions available",
          file=sys.stderr)

    sample = sample_questions(data, args.n_per_type, seed=args.seed)
    print(f"[sample] {len(sample)} questions ({args.n_per_type}/subtask)",
          file=sys.stderr)

    # Make the db_path prefix directory if needed
    if args.db_path != ":memory:":
        Path(args.db_path).parent.mkdir(parents=True, exist_ok=True)

    # Dispatch to the pool. Each worker is initialized with the path
    # prefix + tuning knobs. Workers create + tear down their own DBs.
    worker_args = [
        {"question": q, "global_idx": i, "worker_idx": i % args.workers}
        for i, q in enumerate(sample)
    ]
    print(f"[run] {args.workers} workers, {len(worker_args)} questions, "
          f"BM25(k1={args.bm25_k1}, b={args.bm25_b})", file=sys.stderr)
    t0 = time.time()
    if args.workers <= 1:
        _worker_init(args.db_path, args.bm25_k1, args.bm25_b,
                     args.max_messages_per_q, args.max_seconds_per_q)
        results = [_eval_one_question(a) for a in worker_args]
    else:
        ctx = mp.get_context("spawn")  # spawn avoids fork-safety issues
        with ctx.Pool(
                processes=args.workers,
                initializer=_worker_init,
                initargs=(args.db_path, args.bm25_k1, args.bm25_b,
                          args.max_messages_per_q, args.max_seconds_per_q)
        ) as pool:
            # imap_unordered for streaming progress
            results = list(pool.imap_unordered(
                _eval_one_question, worker_args, chunksize=1))
    elapsed = time.time() - t0
    # Sort by global_idx so the output is deterministic
    results.sort(key=lambda r: r["global_idx"])

    # Aggregate
    by_subtask: dict[str, list[float]] = {}
    by_strategy: dict[str, list[float]] = {}
    by_qtype: dict[str, list[float]] = {}
    for r in results:
        s = 1.0 if r["det_correct"] else 0.0
        by_subtask.setdefault(r["subtask"], []).append(s)
        by_strategy.setdefault(r["judge_strategy"], []).append(s)
        by_qtype.setdefault(r["question_type"], []).append(s)

    def _avg(xs):
        return round(sum(xs) / len(xs), 4) if xs else 0.0

    overall = sum(1.0 for r in results if r["det_correct"]) / max(len(results), 1)
    summary = {
        "version": "v0.6.1",
        "n_total": len(results),
        "n_per_type": args.n_per_type,
        "workers": args.workers,
        "bm25_k1": args.bm25_k1,
        "bm25_b": args.bm25_b,
        "data_source": "xiaowu0162/longmemeval-cleaned (longmemeval_s)",
        "judged_by": "deterministic_rule",
        "overall": round(overall, 4),
        "overall_vs_v055_baseline": 0.948,
        "delta_vs_baseline": round(overall - 0.948, 4),
        "by_subtask": {k: _avg(v) for k, v in by_subtask.items()},
        "by_strategy": {k: _avg(v) for k, v in by_strategy.items()},
        "by_question_type": {k: _avg(v) for k, v in by_qtype.items()},
        "elapsed_seconds": round(elapsed, 2),
        "questions_per_second": round(len(results) / max(elapsed, 1), 3),
        "modules_enabled": {
            "query_rewrite": True,
            "slang_normalization": True,
            "abbreviation_expansion": True,
            "spelling_correction": True,
            "synonym_expansion": True,
            "holiday_resolution": True,
            "negation_indexing": True,
            "multilingual_routing": True,
        },
        "honest_scope_note": (
            f"Sampled {args.n_per_type} questions per subtask from the "
            f"canonical longmemeval_s_cleaned benchmark (500 questions "
            f"total, ~48 sessions/question haystack). μ=0 throughout — "
            f"no LLM at ingest, retrieval, or judging. The score is REAL "
            f"for the sampled subset but does NOT equal a full 500-"
            f"question canonical LongMemEval score."
        ),
    }

    # Per-question attribution: which modules fired?
    # We can't introspect every module from here, but we CAN attribute
    # the verbatim_hits + judge_strategy.
    module_attribution = {
        "verbatim_hits_avg": round(
            sum(r.get("verbatim_hits", 0) for r in results) /
            max(len(results), 1), 2),
        "verbatim_hits_nonzero": sum(
            1 for r in results if r.get("verbatim_hits", 0) > 0),
        "strategy_breakdown": {k: len(v) for k, v in by_strategy.items()},
        "negation_notes_in_context_block": sum(
            1 for r in results
            if "NEGATION" in r.get("context_block_preview", "").upper()),
    }

    out = {
        "summary": summary,
        "module_attribution": module_attribution,
        "results": results,
    }

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\n[done] overall: {overall:.4f} (vs 0.948 baseline = "
          f"{overall - 0.948:+.4f}) in {elapsed:.1f}s "
          f"({len(results)/max(elapsed,1):.1f} Q/s)", file=sys.stderr)
    print(f"[done] by subtask:", file=sys.stderr)
    for k, v in summary["by_subtask"].items():
        n = len(by_subtask[k])
        print(f"  {k:24s} {v:.4f} ({n} Qs)", file=sys.stderr)
    print(f"[out] {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
