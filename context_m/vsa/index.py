"""Page-Clustered Vector Index — hierarchical tree, O(log N) retrieval.

Per the plan (Aeon-style): leaf pages hold up to ``leaf_size`` quantized
vectors; internal nodes store an fp32 centroid + radius (1 - min cos).
Search is best-first over the bound ``centroid_sim - radius`` with exact
scoring inside visited pages only — sub-millisecond at 100K+ vectors
while brute-force scans the whole palace.
"""

from __future__ import annotations

import heapq
import numpy as np

from context_m.vsa.codecs import BaseCodec


class _Node:
    __slots__ = ("centroid", "radius", "children", "rows", "is_leaf")

    def __init__(self) -> None:
        self.centroid: np.ndarray | None = None
        self.radius: float = 1.0
        self.children: list["_Node"] = []
        self.rows: np.ndarray | None = None
        self.is_leaf = True


class TreeIndex:
    def __init__(self, codec: BaseCodec, rows_getter, n: int, *,
                 branch: int = 8, leaf: int = 512, seed: int = 0x0C0FFEE,
                 sample_limit: int = 20000) -> None:
        """``rows_getter(indices) -> (packed, aux)`` fetches codec rows."""
        self.codec = codec
        self.get_rows = rows_getter
        self.n = n
        self.branch = branch
        self.leaf = leaf
        self.seed = seed
        self.sample_limit = sample_limit
        self.root: _Node | None = None
        self.leaf_rows_scanned = 0

    # ------------------------------------------------------------- build
    def build(self) -> None:
        if self.n == 0:
            return
        rng = np.random.default_rng(self.seed)
        self.root = self._build(np.arange(self.n, dtype=np.int64), rng, depth=0)

    def _decode(self, idx: np.ndarray):
        packed, aux = self.get_rows(idx)
        return self.codec.decoded(packed, aux) if self.codec.uses_aux else self.codec.decoded(packed)

    def _build(self, idx: np.ndarray, rng: np.random.Generator, depth: int) -> _Node:
        node = _Node()
        # sample for centroid + kmeans
        take = min(len(idx), self.sample_limit if depth == 0 else 4096)
        sample_idx = idx if len(idx) <= take else idx[rng.choice(len(idx), take, replace=False)]
        sample = self._decode(sample_idx).astype(np.float32)
        centroid = sample.mean(axis=0)
        cn = float(np.linalg.norm(centroid))
        centroid = centroid / cn if cn > 0 else centroid
        node.centroid = centroid
        # EXACT radius w.r.t. this node's own centroid (streamed over all rows)
        max_dist = 0.0
        for i0 in range(0, len(idx), 8192):
            batch = self._decode(idx[i0:i0 + 8192]).astype(np.float32)
            sims = batch @ centroid
            if len(sims):
                max_dist = max(max_dist, float(1.0 - sims.min()))
        node.radius = max_dist + 1e-6
        if len(idx) <= self.leaf or depth > 24:
            node.rows = idx
            node.is_leaf = True
            return node
        k = min(self.branch, len(idx))
        cent = _kmeans(sample, k, rng)
        # assign all rows (streamed)
        parts: list[list[int]] = [[] for _ in range(k)]
        for i0 in range(0, len(idx), 8192):
            batch_idx = idx[i0:i0 + 8192]
            batch = self._decode(batch_idx).astype(np.float32)
            sims = batch @ cent.T                     # (b, k)
            assign = np.argmax(sims, axis=1)
            for j, a in enumerate(assign):
                parts[int(a)].append(int(batch_idx[j]))
        node.is_leaf = False
        for p in parts:
            if p:
                node.children.append(
                    self._build(np.array(p, dtype=np.int64), rng, depth + 1))
        if len(node.children) == 1:
            return node.children[0]
        return node

    # ------------------------------------------------------------ search
    def search(self, q: np.ndarray, k: int, beam: int = 4) -> tuple[np.ndarray, np.ndarray]:
        """Return (row_indices, scores) of approximate top-k."""
        if self.root is None or self.n == 0:
            return np.array([], dtype=np.int64), np.array([], dtype=np.float32)
        qv = self.codec.query_vec(q)
        heap: list[tuple[float, int, _Node]] = []
        counter = 0
        csim = float(qv @ self.root.centroid)
        heapq.heappush(heap, (-(csim - self.root.radius), counter, self.root))
        best: list[tuple[float, int]] = []            # (score, row)
        visited_leaves = 0
        while heap:
            negbound, _, node = heapq.heappop(heap)
            bound = -negbound
            if len(best) >= k and bound <= best[0][0] and visited_leaves >= beam:
                break
            if node.is_leaf:
                rows = node.rows
                packed, aux = self.get_rows(rows)
                sc = (self.codec.scores(packed, q, aux) if self.codec.uses_aux
                      else self.codec.scores(packed, q))
                for r, s in zip(rows.tolist(), sc.tolist()):
                    if len(best) < k:
                        heapq.heappush(best, (float(s), int(r)))
                    elif float(s) > best[0][0]:
                        heapq.heapreplace(best, (float(s), int(r)))
                visited_leaves += 1
                self.leaf_rows_scanned += len(rows)
                if visited_leaves >= max(beam, 1) * 8:
                    break
            else:
                for ch in node.children:
                    c = float(qv @ ch.centroid)
                    heapq.heappush(heap, (-(c - ch.radius), counter := counter + 1, ch))
        order = sorted(best, key=lambda t: -t[0])
        return (np.array([r for _, r in order], dtype=np.int64),
                np.array([s for s, _ in order], dtype=np.float32))


def _kmeans(data: np.ndarray, k: int, rng: np.random.Generator,
            iters: int = 8) -> np.ndarray:
    """Small deterministic k-means (kmeans++ style init)."""
    n = len(data)
    if n <= k:
        return data.copy()
    # kmeans++ init
    cent = [data[rng.integers(n)]]
    d = 1.0 - data @ cent[0]
    d = np.maximum(d, 1e-8)
    for _ in range(1, k):
        probs = d / d.sum()
        cent.append(data[rng.choice(n, p=probs)])
        d = np.minimum(d, np.maximum(1.0 - data @ cent[-1], 1e-8))
    C = np.stack(cent).astype(np.float32)
    for _ in range(iters):
        sims = data @ C.T
        assign = np.argmax(sims, axis=1)
        for j in range(k):
            mask = assign == j
            if mask.any():
                C[j] = data[mask].mean(axis=0)
            else:
                C[j] = data[rng.integers(n)]
        cn = np.linalg.norm(C, axis=1, keepdims=True)
        C = C / np.maximum(cn, 1e-8)
    return C
