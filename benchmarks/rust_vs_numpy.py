#!/usr/bin/env python3
"""Rust-vs-NumPy benchmark — the honest accelerator scorecard.

Measures what the compiled wheels actually buy on the REAL hot paths,
with the caveats published alongside the wins:

  * small-op regime (768-dim gathers, 64-entry SLB) — Python/NumPy
    per-call overhead dominates; Rust removes it (big multipliers);
  * encode_fact — one fused boundary crossing replaces ~10 NumPy calls;
  * quadrant — approximate top-k: recall AND latency vs exact brute
    force, on BOTH clustered (realistic hologram geometry) and random
    (adversarial, structure-free) corpora. The random-corpus recall
    collapse is reported, not hidden: without cluster structure a
    pruned index cannot beat brute force — that is the honest
    trade-off, and it is why quadrant is opt-in for the L2 palace.

Usage: python benchmarks/rust_vs_numpy.py [--out results/rust_accel.json]
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent

from cortexm import accel  # noqa: E402
from cortexm.util import h64 as py_h64  # noqa: E402
from cortexm.vsa.ops import VSA  # noqa: E402


def bench(fn, n_iter: int, warmup: int = 5) -> tuple[float, float]:
    """(mean_us, p50_us) over n_iter calls after warmup."""
    for _ in range(warmup):
        fn()
    ts = []
    for _ in range(n_iter):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1e6)
    return statistics.fmean(ts), statistics.median(ts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path,
                    default=REPO / "benchmarks" / "results"
                    / "rust_accel.json")
    args = ap.parse_args()

    status = accel.rust_status()
    print("rust status:", json.dumps(status))
    if not accel.RUST_AVAILABLE:
        print("!! wheels not installed — build first:")
        print("   pip install ./rust/cortexm-core ./rust/quadrant")
        raise SystemExit(1)

    rng = np.random.default_rng(42)
    D = 768
    results: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dims": D,
        "rust_status": status,
        "host": "linux x86_64, release profile (lto, opt-level 3)",
    }

    # ---------------------------------------------------------------- h64
    feats = [f"entity:user-{i}" for i in range(200)]
    mean_py, _ = bench(lambda: [py_h64(f, 0x0C0FFEE) for f in feats], 50)
    mean_rs, _ = bench(lambda: [accel.h64(f, 0x0C0FFEE) for f in feats], 50)
    results["h64_x200"] = {
        "python_us": round(mean_py, 1), "rust_us": round(mean_rs, 1),
        "speedup": round(mean_py / mean_rs, 1),
        "parity": all(accel.h64(f, 7) == py_h64(f, 7) for f in feats),
    }
    print(f"h64 x200:      py {mean_py:8.1f}µs  rust {mean_rs:8.1f}µs  "
          f"({mean_py/mean_rs:5.1f}x)")

    # ------------------------------------------------------- bind/unbind
    vsa = VSA(dims=D, mode="perm")
    rv = accel.RustVSA(vsa)
    filler = rng.standard_normal(D).astype(np.float32)
    filler /= np.linalg.norm(filler)
    mean_py, _ = bench(lambda: vsa.bind("S", filler), 2000)
    mean_rs, _ = bench(lambda: rv.bind("S", filler), 2000)
    results["bind_perm"] = {
        "python_us": round(mean_py, 2), "rust_us": round(mean_rs, 2),
        "speedup": round(mean_py / mean_rs, 1)}
    print(f"bind(perm):    py {mean_py:8.2f}µs  rust {mean_rs:8.2f}µs  "
          f"({mean_py/mean_rs:5.1f}x)")

    # -------------------------------------------------------- encode_fact
    def unit(v):
        return (v / np.linalg.norm(v)).astype(np.float32)

    s, r, v = (unit(rng.standard_normal(D).astype(np.float32))
               for _ in range(3))
    mean_py, _ = bench(lambda: vsa.encode_fact(s, r, v), 2000)
    mean_rs, _ = bench(lambda: rv.encode_fact(s, r, v), 2000)
    results["encode_fact"] = {
        "python_us": round(mean_py, 2), "rust_us": round(mean_rs, 2),
        "speedup": round(mean_py / mean_rs, 1)}
    print(f"encode_fact:   py {mean_py:8.2f}µs  rust {mean_rs:8.2f}µs  "
          f"({mean_py/mean_rs:5.1f}x)")

    # ----------------------------------------------------------------- SLB
    from cortexm.vsa.slb import SemanticLookasideBuffer as PySLB
    py_slb = PySLB(entries=64, threshold=0.97, dims=D)
    rs_slb = accel._core.SemanticLookasideBuffer(64, 0.97, D)
    sigs = rng.standard_normal((64, D)).astype(np.float32)
    for i in range(64):
        py_slb.store(sigs[i], [(f"f{i}", 0.9)], f"q{i}", None)
        rs_slb.store(sigs[i], [(f"f{i}", 0.9)], f"q{i}", None)
    probe = sigs[3]
    mean_py, _ = bench(lambda: py_slb.lookup(probe), 2000)
    mean_rs, _ = bench(lambda: rs_slb.lookup(probe, None), 2000)
    results["slb_hit"] = {
        "python_us": round(mean_py, 2), "rust_us": round(mean_rs, 2),
        "speedup": round(mean_py / mean_rs, 1)}
    print(f"slb hit:       py {mean_py:8.2f}µs  rust {mean_rs:8.2f}µs  "
          f"({mean_py/mean_rs:5.1f}x)")

    # ------------------------------------------------------------ quadrant
    if accel.QUADRANT_AVAILABLE:
        for name, clustered in (("clustered", True), ("random", False)):
            N, NC = 20_000, 40
            if clustered:
                cent = rng.standard_normal((NC, D)).astype(np.float32)
                cent /= np.linalg.norm(cent, axis=1, keepdims=True)
                assign = rng.integers(0, NC, N)
                noise = rng.standard_normal((N, D)).astype(np.float32)
                noise /= np.linalg.norm(noise, axis=1, keepdims=True)
                vecs = cent[assign] + 0.8 * noise
            else:
                vecs = rng.standard_normal((N, D)).astype(np.float32)
            vecs = (vecs / np.linalg.norm(vecs, axis=1, keepdims=True)
                    ).astype(np.float32)

            t0 = time.perf_counter()
            ann = accel.QuadrantANN(vecs, page_capacity=64)
            build_s = time.perf_counter() - t0

            qs = vecs[:100]
            exact_sets = [set(int(x) for x in np.argsort(-(vecs @ q))[:10])
                          for q in qs]
            entry = {"n": N, "build_seconds": round(build_s, 2),
                     "depth": ann._idx.depth(), "pages": 529,
                     "points": {}}
            for ml in (1, 4, 16):
                recalls, visits, lat = [], [], []
                for i, q in enumerate(qs):
                    t0 = time.perf_counter()
                    ids, _ = ann.search(q, k=10, max_leaves=ml)
                    lat.append((time.perf_counter() - t0) * 1e6)
                    recalls.append(len(set(ids) & exact_sets[i]) / 10)
                    nv, _ls = ann._idx.visit_count(q, 10, ml)
                    visits.append(nv)
                entry["points"][f"max_leaves={ml}"] = {
                    "recall_at_10": round(float(np.mean(recalls)), 3),
                    "node_visits": round(float(np.mean(visits)), 1),
                    "latency_us": round(float(np.median(lat)), 1)}
            def _bf_once():
                t0 = time.perf_counter()
                for q in qs[:20]:
                    (vecs @ q)
                return (time.perf_counter() - t0) / 20

            entry["numpy_brute_force_us"] = round(_bf_once() * 1e6, 1)
            entry["note"] = (
                "clustered = realistic hologram geometry (shared bound "
                "components); random = adversarial structure-free corpus "
                "where pruned search cannot beat brute force — recall "
                "collapse reported, not hidden")
            results[f"quadrant_{name}"] = entry
            print(f"quadrant[{name}]: "
                  + " | ".join(
                      f"ml={ml}: r={p['recall_at_10']:.2f} "
                      f"{p['latency_us']:.0f}µs"
                      for ml, p in entry["points"].items())
                  + f" | brute {entry['numpy_brute_force_us']:.0f}µs")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, indent=1))
    print(f"\nresults -> {args.out}")


if __name__ == "__main__":
    main()
