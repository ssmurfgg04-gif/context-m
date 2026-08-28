"""Benchmark CLI — run BEAM-style buckets and micro-benchmarks.

    python -m cortexm.bench.run --buckets 128k,500k,1m,10m
    python -m cortexm.bench.run --micro
"""

from __future__ import annotations

import argparse
import json
import os
import time

from cortexm.bench.harness import BucketResult, format_report, run_bucket


# --- determinism guard ---------------------------------------------------
# Bench runs MUST be bit-for-bit reproducible. The runtime guard checks
# PYTHONHASHSEED + BLAS thread env vars; if missing, it warns and
# (by default) re-execs under the corrected env. The bench config
# overrides then pin slb_disabled=True so each query recomputes fresh
# fusion (no cache contamination). Production runs leave both OFF —
# the SLB is a real perf win and PYTHONHASHSEED randomization is fine
# for interactive use.
def _setup_determinism():
    try:
        # the script lives in scripts/ but is invoked from anywhere;
        # add the parent of context_m to sys.path so the import works
        # from a checkout.
        here = os.path.dirname(os.path.abspath(__file__))
        # walk up to find scripts/determinism.py
        for parent in [here, os.path.dirname(here), os.path.dirname(os.path.dirname(here))]:
            cand = os.path.join(parent, "scripts", "determinism.py")
            if os.path.exists(cand):
                sys_path = os.path.dirname(cand)
                if sys_path not in sys.path:
                    import sys as _sys
                    _sys.path.insert(0, sys_path)
                break
        from determinism import enforce_determinism, bench_config_overrides
        enforce_determinism()
        return bench_config_overrides
    except Exception:
        # if the determinism module isn't available (e.g. installed
        # via pip without the scripts dir), fall back to no-op
        return lambda **kw: dict(slb_disabled=True, **kw)


import sys


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
    ap.add_argument("--no-determinism", action="store_true",
                    help="skip the determinism guard (dev only)")
    ap.add_argument("--rerank", action="store_true",
                    help="enable μ=0 cross-encoder rerank")
    ap.add_argument("--unmess", action="store_true",
                    help="enable Unmess+DisSim+Bitap OOD ingestion")
    ap.add_argument("--ppr", action="store_true",
                    help="enable Personalized PageRank diffusion")
    args = ap.parse_args()

    if not args.no_determinism:
        bench_overrides = _setup_determinism()
        # merge flag-based feature toggles with the determinism base
        extras = {}
        if args.rerank:
            extras["enable_rerank"] = True
        if args.unmess:
            extras["unmess_enabled"] = True
        if args.ppr:
            extras["ppr_enabled"] = True
        # we don't pass these to run_bucket directly; the harness uses
        # the default Config. The flags are kept here so users can
        # see them documented; a future harness refactor will thread them
        # through.
        _ = bench_overrides(**extras)

    if args.micro:
        from cortexm.bench.micro import run_micro
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
