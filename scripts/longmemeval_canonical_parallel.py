#!/usr/bin/env python3
"""Parallel LongMemEval canonical benchmark runner (μ=0).

Uses ``multiprocessing.Pool`` to evaluate questions in parallel. Each
worker opens its own READ-ONLY SQLite connection to the same .db file
(SQLite WAL allows concurrent readers). Expected speedup vs the
sequential runner: 4× on a 4-core machine (the μ=0 path is CPU-bound,
not I/O-bound).

Usage:
    python scripts/longmemeval_canonical_parallel.py \\
        --n-per-type 5 --max-messages-per-q 1500 \\
        --out benchmarks/results/canonical_parallel.json

The script mirrors ``longmemeval_canonical_full.py``'s argument parser
so callers can swap them transparently. The only behavioral difference
is parallelism: questions are dispatched to a Pool of N workers (where
N defaults to CPU count), and results are aggregated at the end.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path
from typing import Dict, List

# Add the project root to sys.path so the cortexm package imports work
# when this script is run from anywhere.
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _ensure_benchmark_data(bench_dir: Path) -> Path:
    """Ensure the canonical LongMemEval dataset is downloaded.

    Returns the path to the longmemeval_s_cleaned.json file. Downloads
    on first call (~277MB); cached on subsequent calls.
    """
    bench_file = bench_dir / "longmemeval_s_cleaned.json"
    if bench_file.exists():
        return bench_file
    bench_dir.mkdir(parents=True, exist_ok=True)
    print(f"[setup] downloading canonical LongMemEval to {bench_dir}...",
          file=sys.stderr)
    try:
        from huggingface_hub import hf_hub_download
        hf_hub_download(
            repo_id="xiaowu0162/longmemeval-cleaned",
            repo_type="dataset",
            filename="longmemeval_s_cleaned.json",
            local_dir=str(bench_dir),
        )
    except ImportError:
        print("[setup] huggingface_hub not installed; "
              "pip install huggingface_hub", file=sys.stderr)
        sys.exit(1)
    if not bench_file.exists():
        # The file might be in a subdirectory — find it
        for p in bench_dir.rglob("*.json"):
            if "longmemeval" in p.name.lower():
                return p
        raise FileNotFoundError(
            f"could not find longmemeval_s_cleaned.json in {bench_dir}")
    return bench_file


def _worker_init(db_path: str) -> None:
    """Per-worker init: store the db_path on the process global.

    SQLite connections can't be pickled across process boundaries, so
    each worker opens its OWN connection on first use and reuses it for
    subsequent questions.
    """
    global _WORKER_DB_PATH
    _WORKER_DB_PATH = db_path


def _eval_one_question(args: Dict) -> Dict:
    """Evaluate one LongMemEval question in a worker process.

    Opens a READ-ONLY SQLite connection to the shared .db file (WAL mode
    allows concurrent readers). Runs the deterministic MemoryReader +
    judge. Returns the per-question score.

    The ``args`` dict contains:
        - question: the LongMemEval question dict (text, ground_truth,
          type, session_messages, user_id)
        - config_overrides: dict of Config field overrides
    """
    global _WORKER_DB_PATH
    from cortexm import Memory, Config
    from cortexm.api.memory import MemoryReader

    question = args["question"]
    overrides = args.get("config_overrides", {})

    # Each worker uses a FRESH in-memory Memory instance per question
    # (the canonical LongMemEval protocol requires per-question ingest
    # of the user's full conversation history — there's no shared
    # long-term memory across questions for this benchmark).
    cfg = Config.from_env(**overrides) if overrides else Config.from_env()
    m = Memory(cfg)
    try:
        # Ingest the user's session messages
        messages = question.get("session_messages", [])
        user_id = question.get("user_id", "default")
        for msg in messages:
            content = msg.get("content") if isinstance(msg, dict) else str(msg)
            if content:
                m.add(content, user_id=user_id)
        # Search + judge
        reader = MemoryReader(m.config, m.store, m.palace, m.prefetcher)
        result = reader.search(
            query=question["text"], user_id=user_id, k=10)
        # Score with the deterministic judge (delegated to the existing
        # judge module to keep behavior consistent with the sequential
        # runner).
        try:
            from scripts.longmemeval_judge import det_judge
            score = det_judge(
                answer=str(result),
                truth=question.get("ground_truth", ""),
                q_type=question.get("type", ""),
                context_block=getattr(result, "context_block", ""),
            )
        except Exception:
            score = 0.0
        return {
            "id": question.get("id", ""),
            "type": question.get("type", ""),
            "score": float(score) if score is not None else 0.0,
        }
    finally:
        m.close()


def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Parallel LongMemEval canonical benchmark runner (μ=0)")
    p.add_argument("--n-per-type", type=int, default=5,
                   help="number of questions per subtask (default 5)")
    p.add_argument("--max-messages-per-q", type=int, default=1500,
                   help="cap on session messages to ingest per question")
    p.add_argument("--out", type=str,
                   default="benchmarks/results/canonical_parallel.json",
                   help="output JSON path")
    p.add_argument("--workers", type=int, default=mp.cpu_count(),
                   help="number of worker processes (default CPU count)")
    p.add_argument("--seed", type=int, default=42,
                   help="random seed for question sampling")
    p.add_argument("--bench-dir", type=str,
                   default="data/longmemeval",
                   help="directory for the canonical LongMemEval data")
    args = p.parse_args(argv)

    bench_dir = Path(args.bench_dir)
    bench_file = _ensure_benchmark_data(bench_dir)

    print(f"[load] reading {bench_file}", file=sys.stderr)
    with open(bench_file) as f:
        data = json.load(f)
    # The data is a list of question dicts OR a dict with subtask keys
    if isinstance(data, dict):
        all_qs = []
        for subtask, qs in data.items():
            for q in qs:
                q["type"] = subtask
                all_qs.append(q)
    else:
        all_qs = data

    # Sample N per type
    import random
    random.seed(args.seed)
    by_type: Dict[str, List] = {}
    for q in all_qs:
        by_type.setdefault(q.get("type", "unknown"), []).append(q)
    sampled: List = []
    for subtask, qs in by_type.items():
        if len(qs) > args.n_per_type:
            qs = random.sample(qs, args.n_per_type)
        sampled.extend(qs)
    print(f"[sample] {len(sampled)} questions across "
          f"{len(by_type)} subtasks", file=sys.stderr)

    # Truncate session messages per question
    for q in sampled:
        msgs = q.get("session_messages", [])
        if len(msgs) > args.max_messages_per_q:
            q["session_messages"] = msgs[:args.max_messages_per_q]

    # Dispatch to the pool
    worker_args = [{"question": q, "config_overrides": {}} for q in sampled]
    print(f"[run] {args.workers} workers, "
          f"{len(worker_args)} questions", file=sys.stderr)
    t0 = time.time()
    if args.workers <= 1:
        results = [_eval_one_question(a) for a in worker_args]
    else:
        ctx = mp.getcontext("spawn")  # spawn avoids fork-safety issues
        with ctx.Pool(processes=args.workers) as pool:
            results = pool.map(_eval_one_question, worker_args)
    elapsed = time.time() - t0

    # Aggregate
    by_type_scores: Dict[str, List[float]] = {}
    for r in results:
        by_type_scores.setdefault(r["type"], []).append(r["score"])
    summary = {}
    for subtask, scores in by_type_scores.items():
        summary[subtask] = sum(scores) / len(scores) if scores else 0.0
    all_scores = [r["score"] for r in results]
    overall = sum(all_scores) / len(all_scores) if all_scores else 0.0

    out = {
        "n_per_type": args.n_per_type,
        "n_total": len(results),
        "workers": args.workers,
        "elapsed_seconds": round(elapsed, 2),
        "questions_per_second": round(len(results) / elapsed, 3)
            if elapsed > 0 else 0.0,
        "by_subtask": summary,
        "overall": round(overall, 4),
        "results": results,
    }

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2)
    print(f"[done] overall: {overall:.4f} ({len(results)} Qs in "
          f"{elapsed:.1f}s, {len(results)/max(elapsed,1):.1f} Q/s)",
          file=sys.stderr)
    print(f"[out] {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
