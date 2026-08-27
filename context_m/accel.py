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
    from context_m.util import h64  # noqa: F401  (NumPy reference)


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
