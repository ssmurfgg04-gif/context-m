"""SIMD performance benchmark — Rust/PyO3 kernels vs. NumPy reference.

Bench the 6 hot-path operations on production-sized vectors:
  * dot          (768-dim f32 dot product)
  * cosine       (768-dim normalized cosine)
  * dot_i8_f32   (int8 quantized codebook query)
  * batch_dot    (1024 rows × 768 dims matrix-vector)
  * topk         (4096 scores → top-10)
  * argmax       (4096 scores → argmax)

Reports the per-op speedup vs. the equivalent NumPy expression so
the README's "Win — int8 codec + Rust quadrant" claim is verifiable.

Run: python scripts/simd_perf_bench.py
"""
from __future__ import annotations

import sys
import time
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import cortexm.accel as accel


def bench(name: str, fn_rust, fn_np, n_iter: int = 1000,
          warmup: int = 50) -> tuple[float, float]:
    """Run fn_rust() and fn_np() n_iter times each, return mean ms."""
    for _ in range(warmup):
        fn_rust()
        fn_np()
    t0 = time.perf_counter()
    for _ in range(n_iter):
        fn_rust()
    t_rust = (time.perf_counter() - t0) / n_iter * 1000
    t0 = time.perf_counter()
    for _ in range(n_iter):
        fn_np()
    t_np = (time.perf_counter() - t0) / n_iter * 1000
    return t_rust, t_np


def main() -> int:
    rng = np.random.default_rng(seed=42)
    DIMS = 768
    N_ROWS = 1024
    N_SCORES = 4096

    a = rng.standard_normal(DIMS).astype(np.float32)
    b = rng.standard_normal(DIMS).astype(np.float32)
    rows = rng.standard_normal(N_ROWS * DIMS).astype(np.float32)
    a_norm = a / (np.linalg.norm(a) + 1e-9)
    b_norm = b / (np.linalg.norm(b) + 1e-9)
    a_i8 = np.clip(a * 127.0 / (np.max(np.abs(a)) + 1e-9), -127, 127).astype(np.int8)
    scores = rng.standard_normal(N_SCORES).astype(np.float32)

    print("=" * 72)
    print(" Context-M SIMD perf benchmark")
    print("=" * 72)
    print(f"  vectors: {DIMS}-dim f32, batch {N_ROWS} rows, scores {N_SCORES}")
    print()

    cases = [
        ("dot",          lambda: accel.dot(a, b),
                         lambda: float(a @ b)),
        ("cosine",       lambda: accel.cosine(a_norm, b_norm),
                         lambda: float(a_norm @ b_norm)),
        ("dot_i8_f32",   lambda: accel.dot_i8_f32(a_i8, b),
                         lambda: float(np.dot(a_i8.astype(np.float32), b))),
        ("batch_dot",    lambda: accel.batch_dot(rows, b, N_ROWS, DIMS),
                         lambda: (rows.reshape(N_ROWS, DIMS) @ b).tolist()),
        ("topk",         lambda: accel.topk(scores, 10),
                         lambda: sorted(enumerate(scores.tolist()),
                                        key=lambda x: -x[1])[:10]),
        ("argmax",       lambda: accel.argmax(scores),
                         lambda: (int(np.argmax(scores)),
                                  float(scores[np.argmax(scores)]))),
    ]

    print(f"  {'op':<14s}  {'Rust μs':>10s}  {'NumPy μs':>10s}  "
          f"{'speedup':>8s}")
    print("  " + "-" * 60)
    for name, fn_rust, fn_np in cases:
        try:
            t_rust, t_np = bench(name, fn_rust, fn_np, n_iter=200)
            rust_us = t_rust * 1000
            np_us = t_np * 1000
            speedup = t_np / t_rust if t_rust > 0 else 0
            print(f"  {name:<14s}  {rust_us:>10.1f}  {np_us:>10.1f}  "
                  f"{speedup:>7.2f}x")
        except Exception as e:
            print(f"  {name:<14s}  ERROR: {e}")
    print()
    print("  Rust kernels live in rust/cortexm-core/src/simd.rs.")
    print("  Python wrappers in context_m/accel.py auto-detect the")
    print("  best CPU feature set (AVX-512 > AVX2+FMA > NEON > scalar).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
