//! quadrant — page-clustered hierarchical vector index.
//!
//! The L2 "memory palace" scale path promised by the architecture:
//! page-clustered INT8 storage behind a logarithmic-depth 2-means tree.
//!
//! STRUCTURE
//!   * build() recursively splits the corpus with 2-means (deterministic
//!     seeding: pivot 0 + farthest-from-pivot) until each leaf holds at
//!     most `page_capacity` vectors. Depth ≈ log2(N / page_capacity).
//!   * every leaf is a PAGE: a contiguous INT8 block plus one f32 scale —
//!     quantized vectors, asymmetric dequantize-on-score, fixed page size
//!     (capacity × dims) so pages are swappable memory units.
//!
//! SEARCH (best-first, budgeted)
//!   * a max-priority-queue over NODES keyed by centroid similarity;
//!   * pop the most promising node: internal → push children; leaf →
//!     scan its page (exact asymmetric INT8×f32 dots) into the top-k heap;
//!   * stop when `max_leaves` pages were scanned, or the best remaining
//!     node's centroid score is more than `margin` below the k-th best
//!     result (unit-vector radius bound: no vector in that subtree can
//!     beat it);
//!   * visits per query ≈ depth + max_leaves — logarithmic descent with a
//!     constant leaf budget. The visit count is instrumented and
//!     published, not hand-waved.
//!
//! HONEST RECALL CLAIMS: approximate index. Recall@k vs exact brute force
//! is measured and published (`benchmarks/rust_vs_numpy.py`); it is a
//! function of max_leaves/margin and corpus geometry, not a constant.

mod simd;
mod nsg;

use crate::nsg::NsgIndex;
use numpy::PyReadonlyArray1;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::collections::BinaryHeap;

#[derive(Clone)]
struct Page {
    scale: f32,
    packed: Vec<i8>,          // capacity × dims (padded)
    ids: Vec<u32>,            // true ids in this page (≤ capacity)
}

struct Node {
    centroid: Vec<f32>,
    children: Vec<Node>,      // empty for leaves
    page: Option<Page>,
}

impl Node {
    fn is_leaf(&self) -> bool {
        self.children.is_empty()
    }
}

/// (score, node_ref, seq) — BinaryHeap is a max-heap on score; seq breaks
/// ties deterministically.
struct Cand<'a> {
    score: f32,
    seq: u64,
    node: &'a Node,
}

impl PartialEq for Cand<'_> {
    fn eq(&self, other: &Self) -> bool {
        self.score == other.score && self.seq == other.seq
    }
}
impl Eq for Cand<'_> {}
impl PartialOrd for Cand<'_> {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for Cand<'_> {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.score
            .partial_cmp(&other.score)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| self.seq.cmp(&other.seq))
    }
}

struct TopK {
    heap: Vec<(f32, u32)>,    // min-heap (worst at index 0)
    k: usize,
}

impl TopK {
    fn new(k: usize) -> Self {
        TopK { heap: Vec::new(), k }
    }
    fn push(&mut self, score: f32, id: u32) {
        if self.k == 0 {
            return;
        }
        if self.heap.len() < self.k {
            self.heap.push((score, id));
            self.heap.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap());
        } else if score > self.heap[0].0 {
            self.heap[0] = (score, id);
            let mut i = 0;
            loop {
                let l = 2 * i + 1;
                let r = 2 * i + 2;
                let mut m = i;
                if l < self.heap.len() && self.heap[l].0 < self.heap[m].0 {
                    m = l;
                }
                if r < self.heap.len() && self.heap[r].0 < self.heap[m].0 {
                    m = r;
                }
                if m == i {
                    break;
                }
                self.heap.swap(i, m);
                i = m;
            }
        }
    }
    fn threshold(&self) -> f32 {
        self.heap
            .first()
            .map(|t| t.0)
            .unwrap_or(f32::NEG_INFINITY)
    }
    fn sorted(self) -> Vec<(f32, u32)> {
        let mut v = self.heap;
        v.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap());
        v
    }
}

#[inline]
fn dot(a: &[f32], b: &[f32]) -> f32 {
    simd::dot(a, b)
}

#[inline]
fn row<'a>(flat: &'a [f32], dims: usize, i: usize) -> &'a [f32] {
    &flat[i * dims..(i + 1) * dims]
}

/// Deterministic 2-means split of the indexed subset. Returns
/// (centroid_a, ids_a, ids_b) where ids are positions into `all_ids`.
fn two_means(flat: &[f32], dims: usize, positions: &[usize],
             iters: usize) -> (Vec<f32>, Vec<usize>, Vec<usize>) {
    let ca0 = row(flat, dims, positions[0]);
    let mut ca = ca0.to_vec();
    let mut far = positions[0];
    let mut far_sim = f32::INFINITY;
    for &p in positions.iter().skip(1) {
        let s = dot(&ca, row(flat, dims, p));
        if s < far_sim {
            far_sim = s;
            far = p;
        }
    }
    let mut cb = row(flat, dims, far).to_vec();
    let mut ga: Vec<usize> = Vec::new();
    let mut gb: Vec<usize> = Vec::new();
    let n = positions.len();
    for _ in 0..iters {
        ga.clear();
        gb.clear();
        for &p in positions {
            if dot(&ca, row(flat, dims, p)) >= dot(&cb, row(flat, dims, p)) {
                ga.push(p);
            } else {
                gb.push(p);
            }
        }
        if ga.is_empty() || gb.is_empty() {
            ga.clear();
            gb.clear();
            for i in 0..n / 2 {
                ga.push(positions[i]);
            }
            for i in n / 2..n {
                gb.push(positions[i]);
            }
            break;
        }
        let mut new_ca = vec![0f32; ca.len()];
        for &p in &ga {
            for (j, &x) in row(flat, dims, p).iter().enumerate() {
                new_ca[j] += x;
            }
        }
        for x in new_ca.iter_mut() {
            *x /= ga.len() as f32;
        }
        let mut new_cb = vec![0f32; cb.len()];
        for &p in &gb {
            for (j, &x) in row(flat, dims, p).iter().enumerate() {
                new_cb[j] += x;
            }
        }
        for x in new_cb.iter_mut() {
            *x /= gb.len() as f32;
        }
        // converged?
        if dot(&new_ca, &ca) > 0.999_999 && dot(&new_cb, &cb) > 0.999_999 {
            ca = new_ca;
            cb = new_cb;
            break;
        }
        ca = new_ca;
        cb = new_cb;
    }
    (ca, ga, gb)
}

fn build_node(flat: &[f32], dims: usize, positions: &[usize],
              page_capacity: usize) -> Node {
    // centroid of this subtree
    let mut centroid = vec![0f32; dims];
    for &p in positions {
        for (j, &x) in row(flat, dims, p).iter().enumerate() {
            centroid[j] += x;
        }
    }
    for x in centroid.iter_mut() {
        *x /= positions.len() as f32;
    }

    if positions.len() <= page_capacity {
        let mut maxabs = 0f32;
        for &p in positions {
            for &x in row(flat, dims, p) {
                let a = x.abs();
                if a > maxabs {
                    maxabs = a;
                }
            }
        }
        let maxval = 127.0;
        let scale = if maxabs > 0.0 { maxabs / maxval } else { 1.0 };
        let inv = if maxabs > 0.0 { maxval / maxabs } else { 0.0 };
        let mut packed = vec![0i8; page_capacity * dims];
        let ids: Vec<u32> = positions.iter().map(|&p| p as u32).collect();
        for (slot, &p) in positions.iter().enumerate() {
            for (j, &x) in row(flat, dims, p).iter().enumerate() {
                packed[slot * dims + j] =
                    (x * inv).round().clamp(-128.0, 127.0) as i8;
            }
        }
        return Node {
            centroid,
            children: Vec::new(),
            page: Some(Page { scale, packed, ids }),
        };
    }

    let (ca, ga, gb) = two_means(flat, dims, positions, 8);
    // guard: if the split failed to separate, force a halving
    let (ga, gb) = if ga.is_empty() || gb.is_empty() {
        let h = positions.len() / 2;
        (positions[..h].to_vec(), positions[h..].to_vec())
    } else {
        (ga, gb)
    };
    let child_a = build_node(flat, dims, &ga, page_capacity);
    let child_b = build_node(flat, dims, &gb, page_capacity);
    Node {
        centroid,
        children: vec![child_a, child_b],
        page: None,
    }
}

fn tree_depth(n: &Node) -> usize {
    if n.is_leaf() {
        1
    } else {
        1 + n.children.iter().map(tree_depth).max().unwrap()
    }
}

fn count_pages(n: &Node) -> usize {
    if n.is_leaf() {
        1
    } else {
        n.children.iter().map(count_pages).sum()
    }
}

#[pyclass]
struct QuadrantIndex {
    dims: usize,
    page_capacity: usize,
    root: Option<Node>,
    n_vectors: usize,
    depth: usize,
    n_pages: usize,
}

impl QuadrantIndex {
    fn search_impl(&self, q: &[f32], k: usize, max_leaves: usize,
                   margin: f32) -> (Vec<(f32, u32)>, usize, usize) {
        let mut topk = TopK::new(k);
        let mut node_visits = 0usize;
        let mut leaf_scans = 0usize;
        let root = match &self.root {
            Some(r) => r,
            None => return (Vec::new(), 0, 0),
        };
        let mut queue: BinaryHeap<Cand> = BinaryHeap::new();
        queue.push(Cand {
            score: dot(&root.centroid, q),
            seq: 0,
            node: root,
        });
        let mut seq = 1u64;
        while let Some(cand) = queue.pop() {
            node_visits += 1;
            // bound: nothing inside can beat kth - margin
            if leaf_scans >= max_leaves {
                break;
            }
            if cand.score + margin < topk.threshold() {
                break;
            }
            let node = cand.node;
            if node.is_leaf() {
                if let Some(page) = &node.page {
                    let slots = page.ids.len();
                    for slot in 0..slots {
                        let row = &page.packed[slot * self.dims
                            ..(slot + 1) * self.dims];
                        let s = simd::dot_i8_f32(row, q);
                        topk.push(s * page.scale, page.ids[slot]);
                    }
                    leaf_scans += 1;
                }
            } else {
                for child in &node.children {
                    queue.push(Cand {
                        score: dot(&child.centroid, q),
                        seq: {
                            seq += 1;
                            seq
                        },
                        node: child,
                    });
                }
            }
        }
        (topk.sorted(), node_visits, leaf_scans)
    }
}

#[pymethods]
impl QuadrantIndex {
    /// Build from a (N, D) float32 array. `page_capacity` = vectors per
    /// page (leaf); depth comes out ≈ log2(N / page_capacity).
    #[staticmethod]
    #[pyo3(signature = (vectors, page_capacity = 64))]
    fn build(
        py: Python<'_>,
        vectors: numpy::PyReadonlyArray2<f32>,
        page_capacity: usize,
    ) -> PyResult<Self> {
        let arr = vectors.as_array();
        let (n, dims) = (arr.nrows(), arr.ncols());
        if n == 0 {
            return Err(PyValueError::new_err("empty corpus"));
        }
        let flat: Vec<f32> = arr.iter().copied().collect();
        let positions: Vec<usize> = (0..n).collect();
        let cap = page_capacity.max(1);
        let root = build_node(&flat, dims, &positions, cap);
        let depth = tree_depth(&root);
        let n_pages = count_pages(&root);
        Ok(QuadrantIndex {
            dims,
            page_capacity: cap,
            root: Some(root),
            n_vectors: n,
            depth,
            n_pages,
        })
    }

    /// Best-first approximate top-k search.
    /// `max_leaves` = page-scan budget (1 = pure logarithmic descent;
    /// 8-32 typical). `margin` = radius bound for subtree pruning.
    #[pyo3(signature = (query, k = 10, max_leaves = 8, margin = 0.35))]
    fn search(
        &self,
        query: PyReadonlyArray1<f32>,
        k: usize,
        max_leaves: usize,
        margin: f32,
    ) -> PyResult<(Vec<u32>, Vec<f32>)> {
        let q = query.as_slice()?;
        if q.len() != self.dims {
            return Err(PyValueError::new_err("query length != dims"));
        }
        let (out, _, _) =
            self.search_impl(q, k, max_leaves, margin);
        Ok((out.iter().map(|t| t.1).collect(),
            out.iter().map(|t| t.0).collect()))
    }

    /// (node_visits, leaf_scans) for a query — the O(log N) evidence.
    #[pyo3(signature = (query, k = 10, max_leaves = 8, margin = 0.35))]
    fn visit_count(
        &self,
        query: PyReadonlyArray1<f32>,
        k: usize,
        max_leaves: usize,
        margin: f32,
    ) -> PyResult<(usize, usize)> {
        let q = query.as_slice()?;
        if q.len() != self.dims {
            return Err(PyValueError::new_err("query length != dims"));
        }
        let (_, nv, ls) = self.search_impl(q, k, max_leaves, margin);
        Ok((nv, ls))
    }

    fn stats(&self) -> String {
        format!(
            "{{n_vectors: {}, dims: {}, pages: {}, page_capacity: {}, depth: {}}}",
            self.n_vectors, self.dims, self.n_pages, self.page_capacity,
            self.depth
        )
    }

    fn n_vectors(&self) -> usize {
        self.n_vectors
    }

    fn depth(&self) -> usize {
        self.depth
    }
}

#[pymodule]
fn quadrant(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<QuadrantIndex>()?;
    m.add_class::<NsgIndex>()?;
    Ok(())
}
