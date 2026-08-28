"""NSG (Navigating Spreading-out Graph) index backend.

Alternative to the default quadrant page-clustered tree. NSG is more
compact and has better search efficiency at high recall on high-dim
vectors. Trade-off: build is slower (kNN pass), but query latency is
lower at the same recall target.

This module mirrors the pattern from ``cortexm.accel.QuadrantANN`` —
when the Rust wheel is installed (``pip install ./rust/quadrant``), the
hot paths route through the compiled ``NsgIndex``; otherwise a pure-numpy
implementation produces bit-identical recall behavior (it uses the same
MRNG pruning rule, the same medoid-selection logic, and the same greedy
best-first search). The numpy fallback exists so that the NSG backend
ships in production even before the wheel is built for a target platform.

Algorithm references:
  * Fu, Xiang, Wang, Huang — "Fast Approximate Nearest Neighbor Search
    with Navigable Spreading-out Graphs", VLDB 2019.
  * The MRNG (Monotonic Relative Neighborhood Graph) prune rule keeps
    an edge (p, q) iff no other neighbor r of p satisfies
    dist(p, r) < dist(p, q) AND dist(q, r) < dist(p, q).

The numpy fallback uses cosine distance = 1 - dot(a, b) on unit vectors
(which is monotonic in squared Euclidean distance for unit vectors),
matching the Rust side exactly so recall ordering is preserved.
"""

from __future__ import annotations

import os
from typing import Any

import numpy as np

# --- optional Rust wheel ---------------------------------------------------
try:                                            # pragma: no cover
    import quadrant as _quadrant               # type: ignore
except Exception:                               # pragma: no cover
    _quadrant = None

_env = os.environ.get("CONTEXTM_RUST", "auto")
NSG_RUST_AVAILABLE = _quadrant is not None and hasattr(_quadrant, "NsgIndex")
NSG_RUST_ENABLED = (_env == "1") or (_env == "auto" and NSG_RUST_AVAILABLE)


def _sim_to_dist(sim: float) -> float:
    """Convert cosine similarity to MRNG distance.

    For unit vectors, ``1 - dot(a, b)`` is monotonic in squared
    Euclidean distance (``|a-b|^2 = 2 - 2*dot(a, b)``), so it preserves
    the prune-rule ordering the Rust side relies on.
    """
    return 1.0 - float(sim)


class _NsgNumpyFallback:
    """Pure-numpy NSG implementation — same recall behavior as Rust.

    Build is O(N^2 D) (brute kNN), search is greedy best-first with
    frontier width ``ef_search``. Intended for fallback when the wheel
    is not available; the Rust path is preferred for any non-trivial N.
    """

    def __init__(self, k: int = 200, ef_search: int = 64) -> None:
        self.k_build = int(k)
        self.ef_search = int(ef_search)
        self.vectors: np.ndarray | None = None
        self.dims: int = 0
        self.n: int = 0
        self.edges: list[np.ndarray] = []
        self.nav_node: int = 0
        self.n_edges: int = 0
        # ids map node index -> external string id
        self.ids: list[str] = []

    def _medoid(self) -> int:
        """Pick the node with max mean cosine sim to all others.

        Deterministic and avoids RNG. For N <= 1024 we use the full
        O(N^2 D) pass; for larger N we sample 256 candidates × 1024
        eval rows, mirroring the Rust crate's strategy.
        """
        if self.n == 0:
            return 0
        if self.n <= 1024:
            cand = np.arange(self.n)
            eval_set = np.arange(self.n)
        else:
            step_c = max(1, self.n // 256)
            step_e = max(1, self.n // 1024)
            cand = np.arange(0, self.n, step_c)[:256]
            eval_set = np.arange(0, self.n, step_e)[:1024]
        eval_vecs = self.vectors[eval_set]
        best = int(cand[0])
        best_mean = -np.inf
        for c in cand:
            sims = self.vectors[c] @ eval_vecs.T
            mask = eval_set != c
            m = float(sims[mask].mean()) if mask.any() else float(sims.mean())
            if m > best_mean:
                best_mean = m
                best = int(c)
        return best

    def _knn(self, p: int, k: int) -> np.ndarray:
        """kNN of vector p (excluding itself), ascending distance."""
        sims = self.vectors @ self.vectors[p]
        sims[p] = -np.inf  # exclude self
        # Take top-k by sim (highest sim = closest). argpartition + sort
        # gives O(N) partition + O(k log k) sort — same total ordering
        # the Rust side produces via heap selection.
        kk = min(k, self.n - 1)
        if kk <= 0:
            return np.empty(0, dtype=np.int64)
        part = np.argpartition(-sims, kk - 1)[:kk]
        order = part[np.argsort(-sims[part])]
        return order

    def build(self, vectors: np.ndarray, ids: list[str] | None = None) -> None:
        v = np.ascontiguousarray(vectors, dtype=np.float32)
        if v.ndim != 2:
            raise ValueError(f"vectors must be 2-D, got shape {v.shape}")
        n, d = v.shape
        if n == 0:
            raise ValueError("empty corpus")
        # Normalize rows so cosine == dot product. We DO NOT mutate the
        # caller's array; this is the index's own storage.
        norms = np.linalg.norm(v, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        self.vectors = (v / norms).astype(np.float32)
        self.dims = int(d)
        self.n = int(n)
        self.nav_node = self._medoid()
        if ids is None:
            self.ids = [str(i) for i in range(n)]
        else:
            if len(ids) != n:
                raise ValueError(
                    f"len(ids)={len(ids)} != n_vectors={n}")
            self.ids = list(ids)

        # Build pipeline (mirrors Fu et al. VLDB 2019 + the Rust crate):
        #   1. kNN graph (full, pre-prune).
        #   2. MRNG prune rule.
        #   3. Tree-traversal connectivity pass — BFS from the medoid; for
        #      any unreachable node i, add an edge from the nearest visited
        #      kNN of i to i. This guarantees a greedy search starting from
        #      the medoid can reach every node (without it, well-separated
        #      clusters can leave the search stuck in the medoid's cluster).
        k_eff = min(self.k_build, self.n - 1)
        if k_eff <= 0:
            k_eff = 1

        # 1) kNN graph.
        knn_graph: list[np.ndarray] = [self._knn(i, k_eff) for i in range(n)]

        # 2) MRNG prune.
        self.edges = []
        for i in range(n):
            knn = knn_graph[i]
            kept: list[int] = []
            pv = self.vectors[i]
            for q in knn:
                q = int(q)
                qv = self.vectors[q]
                sim_pq = float(pv @ qv)
                d_pq = _sim_to_dist(sim_pq)
                redundant = False
                for r in kept:
                    rv = self.vectors[r]
                    sim_pr = float(pv @ rv)
                    d_pr = _sim_to_dist(sim_pr)
                    if d_pr < d_pq:
                        sim_qr = float(qv @ rv)
                        d_qr = _sim_to_dist(sim_qr)
                        if d_qr < d_pq:
                            redundant = True
                            break
                if not redundant:
                    kept.append(q)
            self.edges.append(np.asarray(kept, dtype=np.int64))

        # 3) Tree-traversal connectivity pass.
        visited = np.zeros(self.n, dtype=bool)
        visited[self.nav_node] = True
        self._bfs_mark(self.nav_node, visited)
        # Iterate until every node is reachable.
        while True:
            unvisited = np.where(~visited)[0]
            if len(unvisited) == 0:
                break
            orphan = int(unvisited[0])
            # Nearest visited neighbor via kNN list (ascending distance).
            added = None
            for cand in knn_graph[orphan]:
                cand = int(cand)
                if visited[cand]:
                    added = cand
                    break
            # Pathological: no visited kNN — fall back to medoid edge.
            src = added if added is not None else self.nav_node
            # Idempotent: don't double-add.
            if orphan not in self.edges[src]:
                self.edges[src] = np.append(self.edges[src], orphan)
            visited[orphan] = True
            self._bfs_mark(orphan, visited)

        self.n_edges = sum(len(e) for e in self.edges)

    def _bfs_mark(self, start: int, visited: np.ndarray) -> None:
        """Mark every node reachable from `start` via `self.edges` as visited."""
        stack = [start]
        while stack:
            node = stack.pop()
            for nb in self.edges[node]:
                nb = int(nb)
                if not visited[nb]:
                    visited[nb] = True
                    stack.append(nb)

    def search(self, query: np.ndarray, k: int = 10) -> list[tuple[str, float]]:
        if self.vectors is None or self.n == 0:
            return []
        q = np.ascontiguousarray(query, dtype=np.float32).ravel()
        if q.shape[0] != self.dims:
            raise ValueError(
                f"query len {q.shape[0]} != dims {self.dims}")
        nq = float(np.linalg.norm(q))
        if nq == 0.0:
            nq = 1.0
        q = q / nq
        ef = max(self.ef_search, k, 1)
        # Greedy best-first from nav_node — same algorithm as Rust side:
        # min-heap on dist = max-heap on -sim; frontier = top-ef by sim;
        # neighbor enters candidate-expansion queue iff it survives the
        # frontier. Terminates when best candidate is worse than the
        # frontier's worst.
        import heapq
        visited = np.zeros(self.n, dtype=bool)
        start = int(self.nav_node)
        start_sim = float(self.vectors[start] @ q)
        candidates: list[tuple[float, int]] = []  # min-heap on dist (1-sim)
        frontier: list[tuple[float, int]] = []  # min-heap on sim (worst at root)
        visited[start] = True
        heapq.heappush(candidates, (1.0 - start_sim, start))
        heapq.heappush(frontier, (start_sim, start))
        while candidates:
            c_dist, c_id = heapq.heappop(candidates)
            c_sim = 1.0 - c_dist
            if len(frontier) == ef and c_sim < frontier[0][0]:
                break
            for nb in self.edges[c_id]:
                nb = int(nb)
                if visited[nb]:
                    continue
                visited[nb] = True
                s = float(self.vectors[nb] @ q)
                # Admit to frontier iff (not full) OR (better than worst)
                admit = (len(frontier) < ef) or (s > frontier[0][0])
                if admit:
                    heapq.heappush(frontier, (s, nb))
                    if len(frontier) > ef:
                        heapq.heappop(frontier)
                    heapq.heappush(candidates, (1.0 - s, nb))
        # Return top-k sorted desc by sim, as (id_str, sim) tuples.
        out = sorted(frontier, key=lambda t: -t[0])[:k]
        return [(self.ids[i], float(s)) for s, i in out]

    def stats(self) -> dict:
        avg_deg = (self.n_edges / self.n) if self.n else 0.0
        return {
            "mode": "numpy-fallback",
            "n_vectors": self.n,
            "dims": self.dims,
            "k_build": self.k_build,
            "n_edges": self.n_edges,
            "avg_degree": round(float(avg_deg), 4),
            "nav_node": self.nav_node,
            "ef_search": self.ef_search,
        }


class NsgBackend:
    """NSG (Navigating Spreading-out Graph) index backend.

    Alternative to the default quadrant page-clustered tree. NSG is more
    compact and has better search efficiency at high recall on high-dim
    vectors. Trade-off: build is slower (kNN pass), but query latency
    is lower at the same recall target.

    Mirrors the pattern of ``cortexm.accel.QuadrantANN``: if the Rust
    wheel (``quadrant.NsgIndex``) is installed and enabled, the hot
    paths route through it; otherwise a pure-numpy NSG produces the same
    recall ordering (the brute kNN pass uses the same selection rule,
    the MRNG prune is bit-identical, the search uses the same greedy
    best-first walk with the same termination criterion).
    """

    def __init__(self, dims: int, k: int = 200, ef_search: int = 64) -> None:
        self.dims = int(dims)
        self.k_build = int(k)
        self.ef_search = int(ef_search)
        self._rust_idx: Any = None
        self._np_idx: _NsgNumpyFallback | None = None
        self._ids: list[str] = []
        self._vectors: np.ndarray | None = None
        self._mode: str = "uninitialized"

    def _use_rust(self) -> bool:
        return NSG_RUST_AVAILABLE and NSG_RUST_ENABLED

    def build(self, vectors: np.ndarray, ids: list[str] | None = None) -> None:
        v = np.ascontiguousarray(vectors, dtype=np.float32)
        if v.ndim != 2:
            raise ValueError(f"vectors must be 2-D, got shape {v.shape}")
        n, d = v.shape
        if d != self.dims:
            raise ValueError(
                f"vectors have dims={d}, expected {self.dims}")
        if n == 0:
            raise ValueError("empty corpus")
        self._vectors = v.copy()
        if ids is None:
            self._ids = [str(i) for i in range(n)]
        else:
            if len(ids) != n:
                raise ValueError(f"len(ids)={len(ids)} != n_vectors={n}")
            self._ids = list(ids)
        if self._use_rust():
            # The Rust wheel owns the vectors and adjacency lists; we
            # keep a reference to the numpy storage so `search()` can
            # resolve ids when callers pass through the Python surface.
            self._rust_idx = _quadrant.NsgIndex.build(v, self.k_build)
            self._np_idx = None
            self._mode = "rust"
        else:                                       # pragma: no cover
            self._rust_idx = None
            self._np_idx = _NsgNumpyFallback(k=self.k_build, ef_search=self.ef_search)
            self._np_idx.build(v, self._ids)
            self._mode = "numpy-fallback"

    def search(self, query: np.ndarray, k: int = 10) -> list[tuple[str, float]]:
        if self._mode == "uninitialized":
            raise RuntimeError("NsgBackend.build() must be called first")
        q = np.ascontiguousarray(query, dtype=np.float32).ravel()
        if q.shape[0] != self.dims:
            raise ValueError(
                f"query len {q.shape[0]} != dims {self.dims}")
        if self._mode == "rust":
            ids_u32, sims = self._rust_idx.search(q, k, self.ef_search)
            return [(self._ids[int(i)], float(s))
                    for i, s in zip(ids_u32, sims)]
        return self._np_idx.search(q, k)              # pragma: no cover

    def stats(self) -> dict:
        if self._mode == "uninitialized":
            return {"mode": "uninitialized", "dims": self.dims,
                    "k_build": self.k_build, "ef_search": self.ef_search}
        if self._mode == "rust":
            return {
                "mode": "rust",
                "dims": self.dims,
                "k_build": self.k_build,
                "ef_search": self.ef_search,
                "detail": self._rust_idx.stats(),
                "n_vectors": int(self._rust_idx.n_vectors()),
                "n_edges": int(self._rust_idx.n_edges()),
                "nav_node": int(self._rust_idx.nav_node()),
            }
        return self._np_idx.stats()                  # pragma: no cover

    def __repr__(self) -> str:
        return (f"NsgBackend(dims={self.dims}, k={self.k_build}, "
                f"ef_search={self.ef_search}, mode={self._mode})")


def nsg_status() -> dict[str, Any]:
    """Report whether the Rust NSG wheel is wired and active."""
    return {
        "rust_available": bool(NSG_RUST_AVAILABLE),
        "rust_enabled": bool(NSG_RUST_ENABLED),
        "env": _env,
        "hint": ("" if NSG_RUST_AVAILABLE else
                 "build with: pip install ./rust/quadrant "
                 "(requires cargo + maturin) — numpy fallback active"),
    }
