"""Modern Hopfield cleanup memory for VSA unbound residuals.

After `vsa.unbind(role, h)` produces a noisy residual h' ≈ true_filler +
noise, this snaps h' to the nearest stored item vector via one-step
modern Hopfield retrieval:

    x ← Σ_i softmax(β · (X^T x)_i) · X_i

Capacity bound (Ramsauer 2020): ~0.14 d^2 patterns for one-shot perfect
recall — overkill for typical codebooks, so we bound the codebook by
memory and run a 1-2 step fixed-point iteration.

Pure numpy, no trained model. The codebook is the universe of subject/
relation/value strings the HashingEmbedder has seen.

arxiv research: arXiv:2409.16408 (HEN), arXiv:2301.10352 (capacity).
"""

from __future__ import annotations

import numpy as np


class HopfieldCleanup:
    """Sparse associative cleanup for unbound VSA residuals.

    Stores a codebook of clean item vectors (subjects, relations, values)
    and snaps noisy residuals back to the nearest stored item.
    """

    def __init__(self, dims: int, beta: float = 8.0, iters: int = 1,
                 max_items: int = 50_000) -> None:
        self.dims = dims
        self.beta = beta
        self.iters = iters
        self.max_items = max_items
        self._items: list[np.ndarray] = []
        self._ids: list[str] = []
        self._mat: np.ndarray | None = None  # (N, d) float32 L2-normed
        self._dirty = False

    def add(self, key: str, vec: np.ndarray) -> None:
        """Add an item to the cleanup codebook. Idempotent on key."""
        if key in set(self._ids):
            return
        if len(self._items) >= self.max_items:
            # LRU-ish: drop oldest 10%
            drop = max(1, len(self._items) // 10)
            self._items = self._items[drop:]
            self._ids = self._ids[drop:]
        v = np.asarray(vec, dtype=np.float32)
        n = float(np.linalg.norm(v))
        v = v / n if n > 0 else v
        self._items.append(v)
        self._ids.append(key)
        self._dirty = True

    def build(self) -> None:
        """Finalize the codebook as a contiguous matrix."""
        if not self._items:
            self._mat = None
            return
        self._mat = np.stack(self._items).astype(np.float32)
        self._dirty = False

    def recall(self, noisy: np.ndarray) -> tuple[str | None, float]:
        """Snap noisy residual to nearest stored item.

        Returns (item_key, similarity). Returns (None, 0.0) if codebook
        is empty.
        """
        if self._mat is None or (self._dirty and self._items):
            self.build()
        if self._mat is None or len(self._mat) == 0:
            return None, 0.0
        x = np.asarray(noisy, dtype=np.float32)
        n = float(np.linalg.norm(x))
        x = x / n if n > 0 else x
        for _ in range(self.iters):
            sims = self._mat @ x  # (N,)
            w = np.exp(self.beta * (sims - sims.max()))
            w = w / w.sum()
            x_new = self._mat.T @ w  # (d,)
            nn = float(np.linalg.norm(x_new))
            x = x_new / nn if nn > 0 else x_new
        # final nearest neighbor
        sims = self._mat @ x
        idx = int(np.argmax(sims))
        return self._ids[idx], float(sims[idx])

    def recall_topk(self, noisy: np.ndarray, k: int = 5
                   ) -> list[tuple[str, float]]:
        """Return top-k candidates after cleanup."""
        if self._mat is None or (self._dirty and self._items):
            self.build()
        if self._mat is None or len(self._mat) == 0:
            return []
        x = np.asarray(noisy, dtype=np.float32)
        n = float(np.linalg.norm(x))
        x = x / n if n > 0 else x
        for _ in range(self.iters):
            sims = self._mat @ x
            w = np.exp(self.beta * (sims - sims.max()))
            w = w / w.sum()
            x_new = self._mat.T @ w
            nn = float(np.linalg.norm(x_new))
            x = x_new / nn if nn > 0 else x_new
        sims = self._mat @ x
        order = np.argsort(-sims)[:k]
        return [(self._ids[int(i)], float(sims[i])) for i in order]

    def __len__(self) -> int:
        return len(self._items)

    def stats(self) -> dict:
        return {
            "items": len(self._items),
            "dims": self.dims,
            "beta": self.beta,
            "iters": self.iters,
            "built": self._mat is not None and not self._dirty,
            "bytes": (self._mat.nbytes if self._mat is not None else 0),
        }


__all__ = ["HopfieldCleanup"]
