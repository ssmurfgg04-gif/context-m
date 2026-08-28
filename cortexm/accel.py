"""Opportunistic Rust acceleration.

The Python/NumPy implementation is the REFERENCE and always works. If the
compiled wheels are installed (`pip install ./rust/cortexm-core` and
`pip install ./rust/quadrant`), the hot paths route through them:

* ``hashing.h64``          — keyed BLAKE2b (byte-exact parity, tested)
* ``vsa.bind/unbind``      — permutation gather with injected perms
* ``vsa.encode_fact``      — fused bind×3 + bundle + lexical mix + norm
* ``slb``                  — L1-resident lookaside buffer
* ``quadrant``             — page-clustered log-depth vector index

Design rule: the Rust side never GENERATES randomness — permutations and
role vectors are injected from Python's deterministic VSA state, so a
mixed Python/Rust deployment produces bit-identical holograms. Every
accelerated path has a NumPy fallback and a parity test
(``tests/test_rust_accel.py``).
"""

from __future__ import annotations

import os
from typing import Any

# --- optional wheels -------------------------------------------------------
try:                                            # pragma: no cover
    import cortexm_core as _core               # type: ignore
except Exception:                               # pragma: no cover
    _core = None

try:                                            # pragma: no cover
    import quadrant as _quadrant               # type: ignore
except Exception:                               # pragma: no cover
    _quadrant = None

RUST_AVAILABLE = _core is not None
QUADRANT_AVAILABLE = _quadrant is not None
_env = os.environ.get("CONTEXTM_RUST", "auto")
RUST_ENABLED = (_env == "1") or (_env == "auto" and RUST_AVAILABLE)
QUADRANT_ENABLED = (_env == "1") or (_env == "auto" and QUADRANT_AVAILABLE)


def rust_status() -> dict[str, Any]:
    return {
        "cortexm_core": bool(RUST_AVAILABLE and RUST_ENABLED),
        "quadrant": bool(QUADRANT_AVAILABLE and QUADRANT_ENABLED),
        "env": _env,
        "hint": ("" if RUST_AVAILABLE else
                 "build with: pip install ./rust/cortexm-core "
                 "./rust/quadrant (requires cargo + maturin)"),
    }


# --- hashing ---------------------------------------------------------------
if RUST_AVAILABLE and RUST_ENABLED:
    def h64(feature: str, seed: int = 0) -> int:
        return _core.h64(feature, seed)
else:                                           # pragma: no cover
    from cortexm.util import h64  # noqa: F401  (NumPy reference)


# --- VSA acceleration wrapper ----------------------------------------------
class RustVSA:
    """Binds a Python VSA to compiled bind/unbind/encode_fact.

    The authoritative perms live in the Python VSA; Rust receives them
    once via set_perm and accelerates the per-fact gathers afterwards.
    """

    def __init__(self, vsa) -> None:
        if not (RUST_AVAILABLE and RUST_ENABLED):
            raise RuntimeError("rust acceleration disabled or unavailable")
        self.vsa = vsa
        self._pb = _core.PermBindings(vsa.dims)
        self._injected: set[str] = set()

    def _inject(self, role: str) -> None:
        if role not in self._injected:
            self._pb.set_perm(role, self.vsa.perm(role).tolist())
            self._injected.add(role)

    def bind(self, role: str, filler):
        import numpy as np
        self._inject(role)
        f = np.ascontiguousarray(filler, dtype=np.float32)
        return self._pb.bind(role, f)

    def unbind(self, role: str, h):
        import numpy as np
        self._inject(role)
        hv = np.ascontiguousarray(h, dtype=np.float32)
        return self._pb.unbind(role, hv)

    def encode_fact(self, s_vec, r_vec, v_vec):
        import numpy as np
        for role in ("S", "R", "V"):
            self._inject(role)
        return self._pb.encode_fact(
            np.ascontiguousarray(s_vec, dtype=np.float32),
            np.ascontiguousarray(r_vec, dtype=np.float32),
            np.ascontiguousarray(v_vec, dtype=np.float32),
            self.vsa.lam)


# --- quadrant index wrapper --------------------------------------------------
class QuadrantANN:
    """Page-clustered log-depth index over holograms (approximate top-k).

    Falls back to exact numpy when the wheel is absent — callers get the
    same interface either way.
    """

    def __init__(self, vectors, page_capacity: int = 64) -> None:
        import numpy as np
        self.vectors = np.ascontiguousarray(vectors, dtype=np.float32)
        if QUADRANT_AVAILABLE and QUADRANT_ENABLED:
            self._idx = _quadrant.QuadrantIndex.build(
                self.vectors, page_capacity)
            self._exact = False
        else:                                    # pragma: no cover
            self._idx = None
            self._exact = True

    def search(self, query, k: int = 10, max_leaves: int = 16):
        import numpy as np
        q = np.ascontiguousarray(query, dtype=np.float32)
        if self._exact:                          # pragma: no cover
            sims = self.vectors @ q
            part = np.argpartition(-sims, k)[:k]
            order = part[np.argsort(-sims[part])]
            return order.tolist(), sims[order].tolist()
        ids, scores = self._idx.search(q, k, max_leaves)
        return list(ids), list(scores)

    def stats(self) -> dict:
        if self._exact:                          # pragma: no cover
            n = self.vectors.shape[0]
            return {"n_vectors": n, "mode": "exact-numpy",
                    "note": "quadrant wheel not installed"}
        return {"mode": "quadrant", "detail": self._idx.stats()}


# --- explicit binary/FP32 tiering ------------------------------------------
# User concern (architectural fix #1): "Binary HRR + FFT doesn't work →
# Separate tiers: binary for edge, FP32 for cloud."
# The codec stack already supports both (binary/RaBitQ for edge, PQ/INT8
# for cloud); this makes the routing EXPLICIT based on deployment tier.

EDGE_CODECS = ("binary", "rabitq")   # 96 B/v — fits on Raspberry Pi 5
CLOUD_CODECS = ("pq", "int8")        # 8 B/v or 770 B/v — bandwidth-dense


def detect_tier() -> str:
    """Auto-detect deployment tier from environment signals.

    Returns 'edge' or 'cloud'. Override via CONTEXTM_TIER env var.
    """
    explicit = os.environ.get("CONTEXTM_TIER", "").lower()
    if explicit in ("edge", "cloud"):
        return explicit
    # heuristic: cloud = multi-core + >4GB RAM + has scipy
    try:
        import multiprocessing
        if multiprocessing.cpu_count() >= 4:
            import os as _os
            mem = _os.sysconf("SC_PAGE_SIZE") * _os.sysconf("SC_PHYS_PAGES")
            if mem >= 4 * 1024**3:
                return "cloud"
    except Exception:
        pass
    return "edge"


def recommend_codec(tier: str | None = None, tmr: bool = True) -> str:
    """Recommend a codec for the deployment tier.

    Edge: binary (with TMR if self-healing required) — 96 B/v.
    Cloud: pq for bandwidth-dense, int8 for accuracy-critical.
    """
    t = tier or detect_tier()
    if t == "edge":
        return "binary" if tmr else "rabitq"
    return "pq"


def tier_status() -> dict:
    """Report current tier + recommended + active codec info."""
    tier = detect_tier()
    rec = recommend_codec(tier)
    return {
        "tier": tier,
        "recommended_codec": rec,
        "edge_codecs": list(EDGE_CODECS),
        "cloud_codecs": list(CLOUD_CODECS),
        "rust_enabled": RUST_ENABLED,
        "quadrant_enabled": QUADRANT_ENABLED,
        "note": (f"Auto-detected {tier} tier; recommend '{rec}' codec. "
                 f"Override with CONTEXTM_TIER=edge|cloud"),
    }


# --- SIMD kernel wrappers (Task 6-simd) ------------------------------------
# The Rust crate exposes `dot / dot_i8_f32 / cosine / l2_sq /
# batch_dot / batch_dot_i8 / topk / argmax` — each is a thin pyo3
# wrapper around the runtime-dispatched kernels in `rust/cortexm-core/
# src/simd.rs` (AVX-512 → AVX2+FMA → NEON → scalar). When the wheel is
# absent we fall back to numpy so callers always get a working answer.
#
# Design rule (mirrors RustVSA): the kernels NEVER allocate randomness
# and produce bit-compatible results across the Rust / NumPy paths (≤1e-5
# FP32 noise, asserted by `tests/test_rust_accel.py::TestSimdKernels`).
# Use the free-function form (`accel.cosine(a, b)`) for one-off queries;
# use the class form (`accel.SimdKernels().batch_dot(...)`) when you
# want to gate behaviour on `RUST_ENABLED` without re-checking globals.


def _as_f32(x) -> "np.ndarray":
    import numpy as np
    return np.ascontiguousarray(x, dtype=np.float32)


def dot(a, b) -> float:
    """SIMD dot product. NumPy fallback: `np.dot(a, b)`."""
    if RUST_AVAILABLE and RUST_ENABLED:
        import numpy as np
        return float(_core.dot(np.ascontiguousarray(a, dtype=np.float32),
                                np.ascontiguousarray(b, dtype=np.float32)))
    import numpy as np
    return float(np.dot(_as_f32(a), _as_f32(b)))


def dot_i8_f32(q8, q) -> float:
    """INT8 × f32 dot product. NumPy fallback casts int8 → f32 first."""
    if RUST_AVAILABLE and RUST_ENABLED:
        import numpy as np
        return float(_core.dot_i8_f32(
            np.ascontiguousarray(q8, dtype=np.int8),
            np.ascontiguousarray(q, dtype=np.float32)))
    import numpy as np
    q8 = np.ascontiguousarray(q8, dtype=np.int8)
    return float((q8.astype(np.float32) @ _as_f32(q)))


def cosine(a, b) -> float:
    """Cosine similarity via SIMD-accelerated dot + L2 norms.

    Identical vectors return exactly 1.0 (bit-exact through the
    SIMD self-dot path); distinct vectors agree with numpy's
    `a @ b / (|a|·|b|)` to within 1e-5 (FP32 lane-reduction noise).
    """
    if RUST_AVAILABLE and RUST_ENABLED:
        import numpy as np
        return float(_core.cosine(
            np.ascontiguousarray(a, dtype=np.float32),
            np.ascontiguousarray(b, dtype=np.float32)))
    import numpy as np
    a = _as_f32(a)
    b = _as_f32(b)
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))


def l2_sq(a, b) -> float:
    """Squared L2 distance `sum((a-b)**2)`. 0.0 for identical vectors."""
    if RUST_AVAILABLE and RUST_ENABLED:
        import numpy as np
        return float(_core.l2_sq(
            np.ascontiguousarray(a, dtype=np.float32),
            np.ascontiguousarray(b, dtype=np.float32)))
    import numpy as np
    d = _as_f32(a) - _as_f32(b)
    return float(d @ d)


def batch_dot(rows, q, n_rows: int, dims: int) -> list[float]:
    """Matrix-vector product over a flat `[n_rows × dims]` f32 slice.

    Far more cache-friendly than calling `dot()` per row from Python —
    one boundary crossing for the whole batch.  Returns a plain list so
    callers can `np.asarray(...)` if they want a contiguous array.
    """
    if RUST_AVAILABLE and RUST_ENABLED:
        import numpy as np
        r = np.ascontiguousarray(rows, dtype=np.float32).reshape(-1)
        q = np.ascontiguousarray(q, dtype=np.float32)
        return list(_core.batch_dot(r, q, n_rows, dims))
    import numpy as np
    r = np.asarray(rows, dtype=np.float32).reshape(n_rows, dims)
    q = _as_f32(q)
    return (r @ q).tolist()


def batch_dot_i8(packed, q, n_rows: int, dims: int) -> list[float]:
    """INT8-packed rows × f32 query.  Raw int8·f32 dot products — callers
    apply per-row scales (the codec's aux array) themselves."""
    if RUST_AVAILABLE and RUST_ENABLED:
        import numpy as np
        p = np.ascontiguousarray(packed, dtype=np.int8).reshape(-1)
        q = np.ascontiguousarray(q, dtype=np.float32)
        return list(_core.batch_dot_i8(p, q, n_rows, dims))
    import numpy as np
    p = np.asarray(packed, dtype=np.int8).reshape(n_rows, dims)
    q = _as_f32(q)
    return (p.astype(np.float32) @ q).tolist()


def topk(scores, k: int) -> list[tuple[int, float]]:
    """Top-`k` (idx, score) tuples in descending order. O(N) via
    `select_nth_unstable`, then O(k log k) for the prefix sort."""
    if RUST_AVAILABLE and RUST_ENABLED:
        import numpy as np
        s = np.ascontiguousarray(scores, dtype=np.float32)
        return [(int(i), float(v)) for i, v in _core.topk(s, k)]
    import numpy as np
    s = np.asarray(scores, dtype=np.float32)
    k = max(0, min(k, s.size))
    if k == 0:
        return []
    part = np.argpartition(-s, k - 1)[:k]
    part = part[np.argsort(-s[part])]
    return [(int(i), float(s[i])) for i in part]


def argmax(scores) -> tuple[int, float]:
    """Return (idx, value) of the max element. Ties → first occurrence."""
    if RUST_AVAILABLE and RUST_ENABLED:
        import numpy as np
        s = np.ascontiguousarray(scores, dtype=np.float32)
        i, v = _core.argmax(s)
        return (int(i), float(v))
    import numpy as np
    s = np.asarray(scores, dtype=np.float32)
    if s.size == 0:
        return (0, 0.0)
    i = int(np.argmax(s))
    return (i, float(s[i]))


class SimdKernels:
    """Container class that mirrors the Rust kernel surface so callers
    can wire `accel.SimdKernels()` once and dispatch uniformly. The
    NumPy fallbacks produce bit-identical results within 1e-5 (FP32
    lane-reduction noise, asserted by parity tests).

    Mirrors the existing `RustVSA` pattern: the constructor raises
    `RuntimeError` if Rust is explicitly disabled (`CONTEXTM_RUST=0`)
    — when Rust is *available* but the user opted out, callers should
    use the free functions above instead.
    """

    def __init__(self) -> None:
        if not (RUST_AVAILABLE and RUST_ENABLED):
            raise RuntimeError(
                "SimdKernels requires Rust acceleration enabled "
                "(CONTEXTM_RUST=auto or =1, and the cortexm_core wheel built)")

    def dot(self, a, b) -> float:
        import numpy as np
        return float(_core.dot(
            np.ascontiguousarray(a, dtype=np.float32),
            np.ascontiguousarray(b, dtype=np.float32)))

    def dot_i8_f32(self, q8, q) -> float:
        import numpy as np
        return float(_core.dot_i8_f32(
            np.ascontiguousarray(q8, dtype=np.int8),
            np.ascontiguousarray(q, dtype=np.float32)))

    def cosine(self, a, b) -> float:
        import numpy as np
        return float(_core.cosine(
            np.ascontiguousarray(a, dtype=np.float32),
            np.ascontiguousarray(b, dtype=np.float32)))

    def l2_sq(self, a, b) -> float:
        import numpy as np
        return float(_core.l2_sq(
            np.ascontiguousarray(a, dtype=np.float32),
            np.ascontiguousarray(b, dtype=np.float32)))

    def batch_dot(self, rows, q, n_rows: int, dims: int) -> list[float]:
        import numpy as np
        r = np.ascontiguousarray(rows, dtype=np.float32).reshape(-1)
        q = np.ascontiguousarray(q, dtype=np.float32)
        return list(_core.batch_dot(r, q, n_rows, dims))

    def batch_dot_i8(self, packed, q, n_rows: int,
                      dims: int) -> list[float]:
        import numpy as np
        p = np.ascontiguousarray(packed, dtype=np.int8).reshape(-1)
        q = np.ascontiguousarray(q, dtype=np.float32)
        return list(_core.batch_dot_i8(p, q, n_rows, dims))

    def topk(self, scores, k: int) -> list[tuple[int, float]]:
        import numpy as np
        s = np.ascontiguousarray(scores, dtype=np.float32)
        return [(int(i), float(v)) for i, v in _core.topk(s, k)]

    def argmax(self, scores) -> tuple[int, float]:
        import numpy as np
        s = np.ascontiguousarray(scores, dtype=np.float32)
        i, v = _core.argmax(s)
        return (int(i), float(v))

