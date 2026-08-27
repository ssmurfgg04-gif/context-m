# Rust workspace — Context-M compiled hot paths

Two crates, both optional accelerators with NumPy fallbacks in
`context_m/accel.py`:

## cortexm-core (`pip install ./rust/cortexm-core`)
- `h64` — keyed BLAKE2b feature hash, **byte-exact parity** with
  `context_m.util.h64` (asserted by tests + the benchmark)
- `PermBindings` — permutation bind/unbind + **fused encode_fact**
  (bind×3 + bundle + lexical mix + normalize in one boundary crossing);
  authoritative perms are INJECTED from Python so holograms stay
  bit-identical across mixed deployments
- `ConvBindings` — HRR circular-convolution bind/unbind via rustfft
- `SemanticLookasideBuffer` — L1-resident 64-entry query cache; results
  cached as PyObjects (hit = refcount bump, zero allocation)
- runtime-dispatched AVX2+FMA SIMD kernels (`simd.rs`) with scalar
  fallback — float reductions do NOT auto-vectorize in LLVM (FP
  associativity), so the kernels are explicit intrinsics

## quadrant (`pip install ./rust/quadrant`)
Page-clustered hierarchical vector index for the L2 memory palace:

- 2-means recursive splits (deterministic seeding), depth ≈ log2(N/page)
- leaves are pages: contiguous INT8 blocks + per-page f32 scale
  (fixed capacity × dims — swappable memory units)
- best-first search: node priority queue by centroid similarity, page
  budget `max_leaves`, radius-bound pruning (`margin`)
- **instrumented visit counts** — the O(log N) claim is measured, not
  asserted: 20k vectors → 529 pages → ~32 nodes visited per query

## Published numbers (this machine, Xeon AVX2)
`benchmarks/results/rust_accel.json` — see `benchmarks/rust_vs_numpy.py`.
Highlights: encode_fact 4.8×, bind 3.4×, h64 2.2×, SLB 1.0× (BLAS already
optimal at 64×768 — published as a tie), quadrant 97% recall@10 at 7×
brute-force speed on clustered hologram geometry. The adversarial
random-corpus recall collapse (0.19) is reported, not hidden.

## Build
```bash
pip install maturin            # once
pip install ./rust/cortexm-core ./rust/quadrant
pytest tests/test_rust_accel.py
python benchmarks/rust_vs_numpy.py
```
The Python/NumPy implementation remains the reference; everything works
without the wheels (`CONTEXTM_RUST=0` forces the pure-Python path).
