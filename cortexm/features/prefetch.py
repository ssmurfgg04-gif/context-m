"""Predictive Memory Prefetching — the Memory Branch Target Buffer.

Learns co-access patterns across retrievals ("agents that asked about X
next asked about Y") and prefetches predicted facts into the fusion
boost set before the next query lands — branch prediction for agent
memory. Wrong predictions cost nothing (a tiny score boost); right
predictions cut effective retrieval latency toward the SLB hit path.
"""

from __future__ import annotations


class Prefetcher:
    def __init__(self, max_pairs: int = 200_000, decay: float = 0.98,
                 min_weight: float = 0.05, window: int = 6) -> None:
        self.max_pairs = max_pairs
        self.decay = decay
        self.min_weight = min_weight
        self.window = window
        self._by_fid: dict[str, dict[str, float]] = {}
        self._recent: list[str] = []
        self._last_predict: dict[str, float] = {}
        self._total_pairs = 0
        self.hits = 0
        self.predictions = 0
        self.predicted_total = 0

    # ------------------------------------------------------------------
    def _bump(self, a: str, b: str) -> None:
        row = self._by_fid.setdefault(a, {})
        prev = row.get(b, 0.0)
        row[b] = prev + 1.0
        if prev == 0.0:
            self._total_pairs += 1

    def observe(self, fact_ids: list[str]) -> None:
        """Called after each retrieval with the delivered fact set."""
        combined = list(dict.fromkeys(
            (self._recent[-self.window:] or []) + list(fact_ids)))
        for i, a in enumerate(combined):
            for b in combined[i + 1:i + 5]:
                if a != b:
                    self._bump(a, b)
                    self._bump(b, a)
        self._recent = list(fact_ids)
        if self._total_pairs > self.max_pairs:
            self._prune()

    def _prune(self) -> None:
        for fid in list(self._by_fid):
            row = self._by_fid[fid]
            for other in list(row):
                row[other] *= self.decay
                if row[other] < self.min_weight:
                    del row[other]
                    self._total_pairs -= 1
            if not row:
                del self._by_fid[fid]

    # ------------------------------------------------------------------
    def predict(self) -> dict[str, float]:
        """Predict next-access facts from recent history (MBTB lookup)."""
        out: dict[str, float] = {}
        for fid in self._recent[-self.window:]:
            row = self._by_fid.get(fid)
            if not row:
                continue
            for other, w in row.items():
                if w >= 1.0:
                    out[other] = max(out.get(other, 0.0), min(1.0, w / 8.0))
        self._last_predict = out
        self.predictions += 1
        self.predicted_total += len(out)
        return out

    def note_hits(self, delivered_ids: list[str]) -> int:
        hits = sum(1 for fid in delivered_ids if fid in self._last_predict)
        self.hits += hits
        return hits

    def stats(self) -> dict:
        return {
            "pairs": self._total_pairs,
            "predictions": self.predictions,
            "prefetch_hits": self.hits,
            "prefetch_hit_ratio": round(self.hits / self.predicted_total, 4)
            if self.predicted_total else 0.0,
        }
