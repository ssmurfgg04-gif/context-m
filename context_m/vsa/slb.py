"""Semantic Lookaside Buffer — conversational locality cache.

64-entry ring buffer of quantized query signatures with cached result
sets. A new query whose signature is ≥ ``threshold`` cosine-similar to a
cached signature reuses the cached ranking (conversational locality:
follow-up questions are near-duplicates of their predecessors). L1-
resident by design; hit path costs one 64×dims dot product.
"""

from __future__ import annotations

import numpy as np


class SemanticLookasideBuffer:
    def __init__(self, entries: int = 64, threshold: float = 0.97,
                 dims: int = 768) -> None:
        self.capacity = entries
        self.threshold = threshold
        self.dims = dims
        self._sigs = np.zeros((entries, dims), dtype=np.float32)
        self._results: list[list[tuple[str, float]] | None] = [None] * entries
        self._queries: list[str | None] = [None] * entries
        self._scopes: list[tuple | None] = [None] * entries
        self._pos = 0
        self._filled = 0
        self.hits = 0
        self.misses = 0
        self.total_hit_latency = 0.0
        self.total_miss_latency = 0.0

    def lookup(self, q: np.ndarray,
               scope: tuple | None = None) -> list[tuple[str, float]] | None:
        """Return cached results for a signature **in the same scope**.

        Scope-blind lookup is a correctness bug, not just a privacy one:
        near-duplicate queries from different users would cross-contaminate
        (and then die in the caller's scope filter, yielding empty blocks).
        """
        if self._filled == 0:
            return None
        sims = self._sigs[: self._filled] @ q
        best = int(np.argmax(sims))
        if float(sims[best]) >= self.threshold and self._scopes[best] == scope:
            self.hits += 1
            return self._results[best]
        return None

    def store(self, q: np.ndarray, results: list[tuple[str, float]],
              query: str = "", scope: tuple | None = None) -> None:
        pos = self._pos
        self._sigs[pos] = q
        self._results[pos] = results
        self._queries[pos] = query
        self._scopes[pos] = scope
        self._pos = (self._pos + 1) % self.capacity
        self._filled = min(self._filled + 1, self.capacity)

    def record_latency(self, hit: bool, seconds: float) -> None:
        if hit:
            self.total_hit_latency += seconds
        else:
            self.total_miss_latency += seconds

    @property
    def miss_latency_avg(self) -> float:
        return (self.total_miss_latency / self.misses) if self.misses else 0.0

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "hits": self.hits, "misses": self.misses,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
            "avg_hit_latency_us": round(
                self.total_hit_latency / self.hits * 1e6, 1) if self.hits else 0.0,
            "avg_miss_latency_us": round(self.miss_latency_avg * 1e6, 1),
            "entries_used": self._filled,
        }
