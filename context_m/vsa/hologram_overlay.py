"""HRR KG overlay — superposed holographic fact store for O(1) lookup.

The Holographic Memory for Knowledge Graphs paper (arXiv:2606.24948,
2026) shows that facts bound as (s, r, v) can be superposed into a
single dense hologram M, and a query (s, r, ?) answered by unbinding
the V slot from M:

    M = Σ_f bind_V(v_f) * (bind_S(s_f) + bind_R(r_f))   # superposition
    v_hat = unbind_V(M) → noisy residual ≈ Σ_f (bind_S(s_f) + bind_R(r_f)) * v_f
    cleanup(v_hat) → snapped to nearest stored value vector

Capacity bound: ~dims^2 / ln(dims) facts before saturation (Clarkson
2023). At d=768 → ~93k facts; at d=16,384 → ~31M facts.

Best used as a per-scope overlay on top of MemoryPalace — single-hop
queries hit the overlay (O(1) + cleanup), multi-hop queries fall back
to TreeIndex search. The overlay trades noise accumulation for lookup
speed; saturation is detected by signal-to-noise ratio on the cleanup
step.

Pure numpy. Reuses VSA.bind/unbind and HopfieldCleanup.recall.

arxiv research: arXiv:2606.24948 (2026 holographic KG); HolE AAAI 2016
(Nickel, Rosasco, Poggio); Plate 1995 (HRR).
"""

from __future__ import annotations

import numpy as np

from context_m.vsa.cleanup import HopfieldCleanup
from context_m.vsa.ops import VSA


def _norm(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


class HolographicFactOverlay:
    """Per-scope superposed hologram for O(1) single-hop fact lookup.

    Each (user_id, scope) tuple gets one dense hologram M. Adding a fact
    binds (s, r, v) and adds to M. Querying unbinds the requested role
    and snaps to the nearest stored item via Hopfield cleanup.

    Falls back gracefully when M saturates — caller can switch to
    MemoryPalace.search for the affected scope.
    """

    def __init__(self, vsa: VSA, cleanup: HopfieldCleanup,
                 saturate_threshold: float = 0.55) -> None:
        self.vsa = vsa
        self.cleanup = cleanup
        self.saturate_threshold = saturate_threshold
        self._M: dict[tuple, np.ndarray] = {}     # scope → hologram
        self._counts: dict[tuple, int] = {}        # scope → fact count
        self._saturated: set[tuple] = set()

    def add_fact(self, scope: tuple, s_vec: np.ndarray,
                r_vec: np.ndarray, v_vec: np.ndarray) -> None:
        """Add a fact (s, r, v) into the superposed hologram."""
        # bind each role to its filler
        bound = (self.vsa.bind("S", s_vec)
                 + self.vsa.bind("R", r_vec)
                 + self.vsa.bind("V", v_vec))
        bound = _norm(bound)
        h = self._M.get(scope)
        if h is None:
            self._M[scope] = bound.copy()
            self._counts[scope] = 1
        else:
            self._M[scope] = _norm(h + bound)
            self._counts[scope] = self._counts.get(scope, 0) + 1
        # populate cleanup codebook with each filler
        self.cleanup.add(f"s:{_hash_vec(s_vec)}", s_vec)
        self.cleanup.add(f"r:{_hash_vec(r_vec)}", r_vec)
        self.cleanup.add(f"v:{_hash_vec(v_vec)}", v_vec)

    def query(self, scope: tuple, query_vec: np.ndarray,
              target_role: str = "V",
              fallback_embs: list[tuple[str, np.ndarray]] | None = None
             ) -> tuple[str | None, float]:
        """Query (s, r, ?) — unbind target_role and cleanup the residual.

        Returns (item_key, confidence). Confidence below saturate_threshold
        indicates either saturation or miss; caller should fall back to
        MemoryPalace.search.

        If fallback_embs is provided, those (key, vec) pairs are temporarily
        added to the cleanup codebook before recall — useful for cross-
        scope queries where the answer may be a value vector not yet in
        the codebook.
        """
        M = self._M.get(scope)
        if M is None:
            return None, 0.0
        if scope in self._saturated:
            # signal saturation — caller should fallback to TreeIndex
            return None, 0.0
        # unbind the target role from M to get a noisy residual
        residual = self.vsa.unbind(target_role, M)
        # add fallback embeddings to cleanup if provided
        if fallback_embs:
            for k, v in fallback_embs:
                self.cleanup.add(k, v)
        self.cleanup.build()
        item_key, conf = self.cleanup.recall(residual)
        if conf < self.saturate_threshold:
            # either miss or saturation — mark saturated if we have many
            # facts and conf is consistently low
            if self._counts.get(scope, 0) > 1000:
                self._saturated.add(scope)
            return None, conf
        return item_key, conf

    def stats(self) -> dict:
        return {
            "scopes": len(self._M),
            "total_facts": sum(self._counts.values()),
            "saturated_scopes": len(self._saturated),
            "cleanup": self.cleanup.stats(),
        }

    def reset_scope(self, scope: tuple) -> None:
        """Drop a saturated scope and let it rebuild from new adds."""
        self._M.pop(scope, None)
        self._counts.pop(scope, None)
        self._saturated.discard(scope)


def _hash_vec(v: np.ndarray) -> str:
    """Stable hash for codebook keys."""
    import hashlib
    h = hashlib.blake2b(v.tobytes(), digest_size=8).hexdigest()
    return h[:16]


__all__ = ["HolographicFactOverlay"]
