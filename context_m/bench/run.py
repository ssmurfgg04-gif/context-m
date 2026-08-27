"""Benchmark CLI — run BEAM-style buckets and micro-benchmarks.

    python -m context_m.bench.run --buckets 128k,500k,1m,10m
    python -m context_m.bench.run --micro
"""

from __future__ import annotations

import argparse
import json
import os
import time

from context_m.bench.harness import BucketResult, format_report, run_bucket


def run_buckets(buckets: list[str], seed: int, out_dir: str,
                db_dir: str | None = None) -> list[BucketResult]:
    os.makedirs(out_dir, exist_ok=True)
    results: list[BucketResult] = []
    for bucket in buckets:
        print(f"\n=== bucket {bucket} ===", flush=True)
        t0 = time.time()
        db = (os.path.join(db_dir, f"bench-{bucket}.db")
              if db_dir else ":memory:")
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
            # a benchmark is always a fresh-corpus run: never let state from
            # a previous run leak in (contaminated reruns silently change
            # fact counts and scores)
            for suffix in ("", "-journal", "-wal", "-shm"):
                stale = db + suffix
                if os.path.exists(stale):
                    os.remove(stale)
        r = run_bucket(bucket, seed=seed, db_path=db)
        results.append(r)
        with open(os.path.join(out_dir, f"{bucket}.json"), "w") as fh:
            json.dump(r.to_dict(), fh, indent=2, default=str)
        cm = r.per_system.get("context_m", {})
        print(f"context_m overall: {cm.get('overall', 0):.1%} "
              f"({r.n_questions} questions, ingest {r.ingest['wall_seconds']}s, "
              f"facts {r.ingest['facts']:,}, μ=0 {r.ingest['u0_protocol']})",
              flush=True)
        print(f"bucket wall time: {time.time() - t0:.1f}s", flush=True)
    report = format_report(results)
    with open(os.path.join(out_dir, "REPORT.md"), "w") as fh:
        fh.write(report)
    print("\nreport written to", os.path.join(out_dir, "REPORT.md"))
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description="Context-M benchmark runner")
    ap.add_argument("--buckets", default="128k",
                    help="comma list: 128k,500k,1m,10m")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=os.path.join("benchmarks", "results"))
    ap.add_argument("--db-dir", default=None,
                    help="persist per-bucket databases here")
    ap.add_argument("--micro", action="store_true",
                    help="run micro-benchmarks instead")
    args = ap.parse_args()

    if args.micro:
        from context_m.bench.micro import run_micro
        out = run_micro()
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, "micro.json"), "w") as fh:
            json.dump(out, fh, indent=2)
        print(json.dumps(out, indent=2)[:4000])
        return

    buckets = [b.strip().lower() for b in args.buckets.split(",") if b.strip()]
    run_buckets(buckets, args.seed, args.out, args.db_dir)


if __name__ == "__main__":
    main()
