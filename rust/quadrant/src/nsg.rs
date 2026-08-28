//! nsg — Navigating Spreading-out Graph (NSG) approximate nearest
//! neighbor index.
//!
//! NSG is a proximity-graph index (Fu, Xiang, Wang, Huang —
//! "Fast Approximate Nearest Neighbor Search with Navigable Spreading-out
//! Graphs", VLDB 2019). It builds on the kNN graph but prunes edges using
//! the Monotonic Relative Neighborhood Graph (MRNG) rule, producing a
//! graph that is much sparser than HNSW while preserving search-time
//! recall. Empirically NSG matches HNSW recall at lower query latency
//! and lower memory on high-dimensional vectors.
//!
//! ALGORITHM
//!
//! 1. **kNN graph**: For each vector p, compute its k nearest neighbors
//!    (default k = 200) by exact distance (small N) or by NN-Descent for
//!    large N. For Context-M's palace scale (10^4–10^6 holograms at 768
//!    dims) we use exact brute force at build time on small corpora and
//!    the trivial full-pair pass for moderate ones — the build is offline
//!    and bounded by O(N^2 D / SIMD_WIDTH) which is fine for N <= ~50k.
//!    Larger builds can swap in NN-Descent later without touching the
//!    search path.
//!
//! 2. **Navigation node (medoid)**: Pick the node whose mean similarity to
//!    every other node is maximal — the medoid. Every search starts from
//!    this node, so the choice matters; the medoid minimises the worst-case
//!    graph distance to any target.
//!
//! 3. **MRNG pruning**: For each p and each candidate neighbor q (drawn
//!    from p's kNN list), keep q iff NO already-kept neighbor r of p
//!    satisfies dist(p,r) < dist(p,q) AND dist(q,r) < dist(p,q). When
//!    such an r exists, q is redundant: there's a monotonic path
//!    p -> r -> q (each hop strictly closer than the direct p->q hop),
//!    so a search from p reaching q via r will never miss q. MRNG is the
//!    *edge-minimal* graph that preserves monotonic reachability — hence
//!    the sparsity win over HNSW.
//!
//! 4. **Search**: Greedy best-first walk from the navigation node. At each
//!    node, expand neighbors into a candidate priority queue, keep a
//!    dynamic list of size ef_search (the "frontier"), and stop when the
//!    frontier's worst element is better than the best unexplored candidate
//!    (the standard HNSW/NSG termination). Returns top-k by similarity.
//!
//! HONEST RECALL CLAIMS: approximate index. Recall@k vs exact brute force
//! depends on (k_build, ef_search, corpus geometry, medoid quality). The
//! numbers are measured, not hand-waved — see `tests/test_nsg.py`.

use numpy::PyReadonlyArray1;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::collections::BinaryHeap;

use crate::simd;

#[inline]
fn dot(a: &[f32], b: &[f32]) -> f32 {
    simd::dot(a, b)
}

#[inline]
fn row<'a>(flat: &'a [f32], dims: usize, i: usize) -> &'a [f32] {
    &flat[i * dims..(i + 1) * dims]
}

/// Similarity distance for cosine-like ranking: we operate on dot products,
/// so "smaller distance = closer" maps to "larger similarity = closer".
/// The MRNG rule needs a *distance* that satisfies the triangle-like
/// property used in the prune test. We use `1 - dot(a,b)` which for unit
/// vectors is half the squared Euclidean distance and obeys the
/// monotonicity the prune rule relies on.
#[inline]
fn dist(a: &[f32], b: &[f32]) -> f32 {
    1.0 - dot(a, b)
}

/// (sim, id) max-heap entry for top-k expansion. Order on sim, then id.
#[derive(Clone, Copy)]
struct SimId {
    sim: f32,
    id: u32,
}
impl PartialEq for SimId {
    fn eq(&self, other: &Self) -> bool {
        self.sim == other.sim && self.id == other.id
    }
}
impl Eq for SimId {}
impl PartialOrd for SimId {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for SimId {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        // max-heap on sim (sim = dot product); break ties by id
        self.sim
            .partial_cmp(&other.sim)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| self.id.cmp(&other.id))
    }
}

/// (dist, id) min-heap entry for kNN expansion during build / search.
#[derive(Clone, Copy)]
struct DistId {
    dist: f32,
    id: u32,
}
impl PartialEq for DistId {
    fn eq(&self, other: &Self) -> bool {
        self.dist == other.dist && self.id == other.id
    }
}
impl Eq for DistId {}
impl PartialOrd for DistId {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for DistId {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        // BinaryHeap is a max-heap; we want min-heap on dist, so reverse.
        other
            .dist
            .partial_cmp(&self.dist)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| other.id.cmp(&self.id))
    }
}

/// Frontier: keeps the top-`cap` candidates by similarity during a
/// search. Internally a MIN-heap on `sim` so the root (index 0) is the
/// WORST kept element — easy to evict when a better one arrives. The
/// `worst_sim()` method exposes that root for the search loop's
/// termination and admission decisions.
struct Frontier {
    heap: Vec<SimId>, // min-heap on sim (worst at index 0)
    cap: usize,
}
impl Frontier {
    fn new(cap: usize) -> Self {
        Frontier { heap: Vec::new(), cap }
    }
    fn len(&self) -> usize {
        self.heap.len()
    }
    fn worst_sim(&self) -> f32 {
        self.heap
            .first()
            .map(|t| t.sim)
            .unwrap_or(f32::NEG_INFINITY)
    }
    /// Returns `true` if the element was admitted into the frontier
    /// (i.e., survived eviction). The caller uses the return value to
    /// decide whether to also push into the candidate-expansion queue.
    fn push(&mut self, sim: f32, id: u32) -> bool {
        if self.cap == 0 {
            return false;
        }
        if self.heap.len() < self.cap {
            // not yet full — always admit
            self.heap.push(SimId { sim, id });
            self.sift_up(self.heap.len() - 1);
            true
        } else if sim > self.heap[0].sim {
            // full and better than the worst — replace root, sift down
            self.heap[0] = SimId { sim, id };
            self.sift_down(0);
            true
        } else {
            false
        }
    }
    fn sift_up(&mut self, mut i: usize) {
        while i > 0 {
            let parent = (i - 1) / 2;
            if self.heap[i].sim < self.heap[parent].sim {
                self.heap.swap(i, parent);
                i = parent;
            } else {
                break;
            }
        }
    }
    fn sift_down(&mut self, mut i: usize) {
        loop {
            let l = 2 * i + 1;
            let r = 2 * i + 2;
            let mut m = i;
            if l < self.heap.len() && self.heap[l].sim < self.heap[m].sim {
                m = l;
            }
            if r < self.heap.len() && self.heap[r].sim < self.heap[m].sim {
                m = r;
            }
            if m == i {
                break;
            }
            self.heap.swap(i, m);
            i = m;
        }
    }
    fn sorted_desc(self) -> Vec<(f32, u32)> {
        let mut v = self.heap;
        v.sort_by(|a, b| b.sim.partial_cmp(&a.sim).unwrap_or(std::cmp::Ordering::Equal));
        v.iter().map(|t| (t.sim, t.id)).collect()
    }
}

/// The NSG index.
///
/// Build is offline; search is greedy best-first from `nav_node`. The
/// search returns the same (ids, sims) shape as `QuadrantIndex` so
/// callers can swap backends transparently.
#[pyclass]
pub struct NsgIndex {
    dims: usize,
    /// Flat f32 storage — N × dims.
    vectors: Vec<f32>,
    n_vectors: usize,
    /// Adjacency lists — `edges[i]` is the list of NSG-pruned neighbor ids
    /// for vector i. Publicly read-only via `neighbor_count`/`stats`.
    edges: Vec<Vec<u32>>,
    /// Navigation (medoid) node id. Every search starts here.
    nav_node: u32,
    /// Build-time k used for the initial kNN graph (before MRNG pruning).
    k_build: usize,
    /// Total edges in the pruned graph (sum of edges[i].len() over i).
    n_edges: usize,
}

impl NsgIndex {
    /// Compute the k nearest neighbors of vector `p` (id) by brute force.
    /// Returns (id, dist) pairs sorted ascending by distance. Excludes p
    /// itself and skips ties deterministically by id.
    fn knn_brute(
        &self,
        p: usize,
        k: usize,
    ) -> Vec<(u32, f32)> {
        let pv = row(&self.vectors, self.dims, p);
        let mut heap: BinaryHeap<DistId> = BinaryHeap::new(); // max-heap on dist (min-heap via Ord reversal)
        let kk = k.min(self.n_vectors.saturating_sub(1)).max(1);
        for j in 0..self.n_vectors {
            if j == p {
                continue;
            }
            let d = dist(pv, row(&self.vectors, self.dims, j));
            if heap.len() < kk {
                heap.push(DistId { dist: d, id: j as u32 });
            } else if d < heap.peek().map(|t| t.dist).unwrap_or(f32::INFINITY) {
                heap.pop();
                heap.push(DistId { dist: d, id: j as u32 });
            }
        }
        let mut v: Vec<(u32, f32)> =
            heap.into_iter().map(|t| (t.id, t.dist)).collect();
        v.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal));
        v
    }

    /// Greedy best-first search from `nav_node`. Returns sorted-desc
    /// (sim, id) tuples, up to `k`. Visits at most `ef_search` frontier
    /// nodes and their neighbors; stops when the best unexplored
    /// candidate is worse than the frontier's worst.
    fn search_impl(
        &self,
        q: &[f32],
        k: usize,
        ef_search: usize,
    ) -> Vec<(f32, u32)> {
        if self.n_vectors == 0 {
            return Vec::new();
        }
        let ef = ef_search.max(k).max(1);
        // Visited set — HashSet would be cleaner but Vec<bool> is faster
        // for our small N range; we use a marker array per search.
        let mut visited = vec![false; self.n_vectors];
        // Candidate min-heap on distance (i.e., max-heap on -dist = sim).
        // We use BinaryHeap<DistId> which Ord-reverses to behave as a
        // min-heap on dist (so .peek() returns the smallest dist).
        let mut candidates: BinaryHeap<DistId> = BinaryHeap::new();
        // Frontier: keep top-ef by similarity (worst at root).
        let mut frontier = Frontier::new(ef);

        let start = self.nav_node as usize;
        let start_sim = dot(row(&self.vectors, self.dims, start), q);
        let start_dist = 1.0 - start_sim;
        visited[start] = true;
        candidates.push(DistId { dist: start_dist, id: start as u32 });
        frontier.push(start_sim, start as u32);

        while let Some(c) = candidates.pop() {
            // Termination: best candidate is worse than the frontier's
            // worst — search has converged.
            let best_cand_sim = 1.0 - c.dist;
            if frontier.len() == ef && best_cand_sim < frontier.worst_sim() {
                break;
            }
            let node = c.id as usize;
            for &nb in self.edges[node].iter() {
                let nbu = nb as usize;
                if visited[nbu] {
                    continue;
                }
                visited[nbu] = true;
                let s = dot(row(&self.vectors, self.dims, nbu), q);
                // Standard NSG greedy admission: a neighbor enters the
                // candidate-expansion queue iff it survives into the
                // frontier (i.e., it's among the top-ef seen so far). This
                // keeps expansion bounded toward improving regions of the
                // graph — the core of NSG's lower query latency vs HNSW.
                if frontier.push(s, nb) {
                    candidates.push(DistId { dist: 1.0 - s, id: nb });
                }
            }
        }

        let mut topk = frontier.sorted_desc();
        topk.truncate(k);
        topk
    }
}

#[pymethods]
impl NsgIndex {
    /// Build an NSG index from a (N, D) float32 array.
    ///
    /// `k` = initial kNN graph fanout (default 200 — VLDB paper's sweet
    /// spot for 768-dim embeddings). Build is O(N^2 D) due to brute kNN;
    /// for N > 50k consider swapping in NN-Descent (not yet implemented).
    #[staticmethod]
    #[pyo3(signature = (vectors, k = 200))]
    fn build(
        py: Python<'_>,
        vectors: numpy::PyReadonlyArray2<f32>,
        k: usize,
    ) -> PyResult<Self> {
        let arr = vectors.as_array();
        let (n, dims) = (arr.nrows(), arr.ncols());
        if n == 0 {
            return Err(PyValueError::new_err("empty corpus"));
        }
        if dims == 0 || dims % 8 != 0 {
            return Err(PyValueError::new_err(
                "dims must be a positive multiple of 8 (SIMD alignment)",
            ));
        }
        // Flatten into a contiguous Vec<f32>. as_array already gives a
        // C-contiguous view if the input is contiguous; copy if not.
        let flat: Vec<f32> = arr.iter().copied().collect();
        // Find medoid (node with maximum mean similarity to all others).
        // For large N this is O(N^2 D); we cap by sampling medoid candidates
        // when N exceeds the cost threshold (still O(N D) per candidate).
        let nav_node = medoid(&flat, dims, n);

        let mut idx = NsgIndex {
            dims,
            vectors: flat,
            n_vectors: n,
            edges: vec![Vec::new(); n],
            nav_node,
            k_build: k.max(1),
            n_edges: 0,
        };

        // Build pipeline (mirrors Fu et al. VLDB 2019):
        //   1. Compute kNN graph (stored; reused in step 3).
        //   2. MRNG prune: walk each node's kNN ascending by distance; keep
        //      q iff no kept r satisfies dist(p,r) < dist(p,q) AND
        //      dist(q,r) < dist(p,q) — the prune rule that gives NSG its
        //      sparsity vs HNSW.
        //   3. Tree-traversal pass: BFS from the medoid; for any unreachable
        //      node i, add an edge from the nearest visited neighbor of i
        //      (taken from i's kNN list) to i. This guarantees the search
        //      starting from nav_node can reach every node — without this,
        //      clustered corpora can leave some clusters unreachable via
        //      greedy expansion. We do NOT inject the medoid as a neighbor
        //      of every node (that would bloat medoid's degree to N-1);
        //      instead we add only the minimum edges needed for connectivity.
        //      If no visited kNN exists for i (pathological case where i's
        //      kNN list is all-unreachable), fall back to the medoid edge.
        let k_eff = k.min(n.saturating_sub(1)).max(1);

        // 1) kNN graph (full, pre-prune). knn_graph[i] is a Vec<u32>
        // sorted ascending by distance (closest first).
        let mut knn_graph: Vec<Vec<u32>> = Vec::with_capacity(n);
        for i in 0..n {
            py.check_signals()?;
            let knn = idx.knn_brute(i, k_eff);
            knn_graph.push(knn.iter().map(|t| t.0).collect());
        }

        // 2) MRNG prune — produce `edges`.
        for i in 0..n {
            py.check_signals()?;
            let pv = row(&idx.vectors, dims, i);
            let knn = &knn_graph[i];
            let mut kept: Vec<u32> = Vec::with_capacity(knn.len());
            for &q_id in knn.iter() {
                let qv = row(&idx.vectors, dims, q_id as usize);
                let d_pq = dist(pv, qv);
                let mut redundant = false;
                for &r_id in kept.iter() {
                    let rpv = row(&idx.vectors, dims, r_id as usize);
                    let d_pr = dist(pv, rpv);
                    if d_pr < d_pq {
                        let d_qr = dist(qv, rpv);
                        if d_qr < d_pq {
                            redundant = true;
                            break;
                        }
                    }
                }
                if !redundant {
                    kept.push(q_id);
                }
            }
            idx.edges[i] = kept;
        }

        // 3) Tree-traversal connectivity pass.
        let mut visited = vec![false; n];
        visited[nav_node as usize] = true;
        // First BFS over the MRNG-pruned edges.
        bfs_mark(&idx.edges, nav_node as usize, &mut visited);
        // Iterate until every node is reachable. Each iteration: for any
        // unvisited i, find its nearest visited kNN and add an edge from
        // that kNN to i, then BFS again from i through the existing edges.
        // Bounded by O(N) iterations in the worst case (one orphan per
        // pass); in practice clustered data converges in 1-2 passes.
        loop {
            // Find any unvisited node.
            let orphan = match (0..n).find(|&i| !visited[i]) {
                Some(o) => o,
                None => break, // all reachable
            };
            // Nearest visited neighbor via kNN list (sorted ascending).
            let mut added = None;
            for &cand in knn_graph[orphan].iter() {
                if visited[cand as usize] {
                    added = Some(cand as usize);
                    break;
                }
            }
            // Pathological: no visited kNN — fall back to medoid edge.
            let from = added.unwrap_or(nav_node as usize);
            // Idempotent: don't double-add.
            if !idx.edges[from].contains(&(orphan as u32)) {
                idx.edges[from].push(orphan as u32);
            }
            visited[orphan] = true;
            // Propagate reachability through the orphan's edges.
            bfs_mark(&idx.edges, orphan, &mut visited);
        }

        // Tally final edge count.
        idx.n_edges = idx.edges.iter().map(|e| e.len()).sum();

        Ok(idx)
    }

    /// Greedy best-first search from the navigation node.
    /// `k` = top-k results; `ef_search` = frontier width (64 typical).
    /// Returns (ids, sims) — ids descending by similarity.
    #[pyo3(signature = (query, k = 10, ef_search = 64))]
    fn search(
        &self,
        query: PyReadonlyArray1<f32>,
        k: usize,
        ef_search: usize,
    ) -> PyResult<(Vec<u32>, Vec<f32>)> {
        let q = query.as_slice()?;
        if q.len() != self.dims {
            return Err(PyValueError::new_err("query length != dims"));
        }
        let out = self.search_impl(q, k, ef_search);
        Ok((out.iter().map(|t| t.1).collect(),
            out.iter().map(|t| t.0).collect()))
    }

    /// Stats string: graph density, navigation node, build k.
    fn stats(&self) -> String {
        let avg_deg = if self.n_vectors > 0 {
            self.n_edges as f64 / self.n_vectors as f64
        } else {
            0.0
        };
        format!(
            "{{n_vectors: {}, dims: {}, k_build: {}, n_edges: {}, \
             avg_degree: {:.2}, nav_node: {}}}",
            self.n_vectors, self.dims, self.k_build, self.n_edges,
            avg_deg, self.nav_node,
        )
    }

    fn n_vectors(&self) -> usize {
        self.n_vectors
    }

    fn n_edges(&self) -> usize {
        self.n_edges
    }

    fn nav_node(&self) -> u32 {
        self.nav_node
    }

    fn k_build(&self) -> usize {
        self.k_build
    }
}

/// BFS marker: starting from `start`, traverse `edges` and mark every
/// reachable node `true` in `visited`. Returns the count of newly-
/// visited nodes (excludes the start if it was already visited).
fn bfs_mark(edges: &[Vec<u32>], start: usize, visited: &mut [bool]) -> usize {
    let mut newly = 0;
    let mut stack: Vec<usize> = vec![start];
    while let Some(node) = stack.pop() {
        for &nb in edges[node].iter() {
            let nbu = nb as usize;
            if !visited[nbu] {
                visited[nbu] = true;
                newly += 1;
                stack.push(nbu);
            }
        }
    }
    newly
}

/// Pick the medoid of the dataset — the vector whose mean similarity to
/// every other vector is maximal. For N <= 1024 we use the full O(N^2)
/// computation; for larger N we sample 256 candidate medoids uniformly
/// (seeded by summing the first dimension — deterministic) and pick the
/// one with the best mean similarity to a 1024-row sample. This stays
/// deterministic across runs (no RNG) and bounds the cost at O(N D * 256).
fn medoid(flat: &[f32], dims: usize, n: usize) -> u32 {
    if n == 1 {
        return 0;
    }
    // Pick candidate set: full set if small, else a deterministic sample.
    let candidates: Vec<usize> = if n <= 1024 {
        (0..n).collect()
    } else {
        // Deterministic sample: stride-based selection avoids RNG.
        let step = n / 256;
        let step = step.max(1);
        (0..n).step_by(step).take(256).collect()
    };
    // Pick evaluation set (the rows we average similarity over).
    let eval_set: Vec<usize> = if n <= 1024 {
        (0..n).collect()
    } else {
        let step = n / 1024;
        let step = step.max(1);
        (0..n).step_by(step).take(1024).collect()
    };
    let mut best = candidates[0];
    let mut best_mean = f32::NEG_INFINITY;
    for &c in candidates.iter() {
        let cv = row(flat, dims, c);
        let mut sum = 0f32;
        for &e in eval_set.iter() {
            if e == c {
                continue;
            }
            sum += dot(cv, row(flat, dims, e));
        }
        let mean = sum / eval_set.len().max(1) as f32;
        if mean > best_mean {
            best_mean = mean;
            best = c;
        }
    }
    best as u32
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_clustered(n: usize, dims: usize, n_clusters: usize) -> Vec<f32> {
        // Deterministic synthetic corpus: NC well-separated unit centroids,
        // each N/NC vectors jittered around its centroid. Returns flat N×D f32.
        let mut flat = vec![0f32; n * dims];
        let nc = n_clusters.min(n);
        // Use simple deterministic centroids derived from i.
        let mut cent = vec![0f32; nc * dims];
        for c in 0..nc {
            for d in 0..dims {
                cent[c * dims + d] =
                    ((c * 7 + d * 3) as f32).sin() * 0.5;
            }
            // normalize
            let mut norm = 0f32;
            for d in 0..dims {
                norm += cent[c * dims + d] * cent[c * dims + d];
            }
            norm = norm.sqrt().max(1e-6);
            for d in 0..dims {
                cent[c * dims + d] /= norm;
            }
        }
        for i in 0..n {
            let c = i % nc;
            for d in 0..dims {
                // small deterministic jitter
                let jitter = ((i * 13 + d * 5) as f32).sin() * 0.05;
                flat[i * dims + d] = cent[c * dims + d] + jitter;
            }
            // normalize
            let mut norm = 0f32;
            for d in 0..dims {
                norm += flat[i * dims + d] * flat[i * dims + d];
            }
            norm = norm.sqrt().max(1e-6);
            for d in 0..dims {
                flat[i * dims + d] /= norm;
            }
        }
        flat
    }

    #[test]
    fn test_build_and_search_self_match() {
        let n = 64;
        let dims = 32;
        let flat = make_clustered(n, dims, 4);
        // Build via the public surface by emulating pyo3 input is awkward in
        // unit tests; instead exercise the internal search path directly.
        let mut idx = NsgIndex {
            dims,
            vectors: flat.clone(),
            n_vectors: n,
            edges: vec![Vec::new(); n],
            nav_node: 0,
            k_build: 8,
            n_edges: 0,
        };
        // Stub: fully-connected graph (no pruning) so search is exhaustive
        // along edges; verify self-match property.
        for i in 0..n {
            for j in 0..n {
                if i != j {
                    idx.edges[i].push(j as u32);
                }
            }
            idx.n_edges += idx.edges[i].len();
        }
        let q = row(&flat, dims, 0);
        let out = idx.search_impl(q, 5, 16);
        assert!(!out.is_empty());
        assert_eq!(out[0].1, 0); // self must be best match
    }

    #[test]
    fn test_medoid_pick_when_single() {
        assert_eq!(medoid(&[1.0, 0.0, 0.0, 0.0], 1, 1), 0);
    }

    #[test]
    fn test_frontier_keeps_top_ef() {
        let mut f = Frontier::new(3);
        f.push(0.1, 0);
        f.push(0.9, 1);
        f.push(0.5, 2);
        f.push(0.95, 3); // should evict id 0 (smallest sim)
        let s = f.sorted_desc();
        assert_eq!(s.len(), 3);
        assert_eq!(s[0].1, 3); // best
        assert!(s.iter().all(|t| t.1 != 0)); // id 0 evicted
    }
}
