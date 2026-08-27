//! Semantic Lookaside Buffer — the L1-resident query cache.
//!
//! Parity with `context_m.vsa.slb.SemanticLookasideBuffer`: 64-entry ring
//! of f32 signatures; a lookup is one 64×dims matvec + argmax + threshold.
//! The hit path is branch-light and cache-friendly (64×768×4B = 192 KiB —
//! L2-resident; the hot rows stay in L1).

use numpy::PyReadonlyArray1;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

#[pyclass]
pub struct SemanticLookasideBuffer {
    capacity: usize,
    dims: usize,
    threshold: f32,
    sigs: Vec<f32>,          // capacity × dims, row-major
    results: Vec<Option<Py<PyAny>>>,   // cached result objects (built once
                                       // at store; hits return a reference)
    queries: Vec<Option<String>>,
    scopes: Vec<Option<String>>,    // JSON-encoded scope tuple
    pos: usize,
    filled: usize,
    hits: u64,
    misses: u64,
}

#[pymethods]
impl SemanticLookasideBuffer {
    #[new]
    #[pyo3(signature = (entries = 64, threshold = 0.97, dims = 768))]
    fn new(entries: usize, threshold: f32, dims: usize) -> Self {
        SemanticLookasideBuffer {
            capacity: entries,
            dims,
            threshold,
            sigs: vec![0.0; entries * dims],
            results: (0..entries).map(|_| None).collect(),
            queries: (0..entries).map(|_| None).collect(),
            scopes: (0..entries).map(|_| None).collect(),
            pos: 0,
            filled: 0,
            hits: 0,
            misses: 0,
        }
    }

    /// Returns the cached [(id, score), ...] on a signature hit in the
    /// same scope, else None. One matvec + argmax — the whole point.
    #[pyo3(signature = (q, scope=None))]
    fn lookup(
        &mut self,
        q: PyReadonlyArray1<f32>,
        scope: Option<String>,
    ) -> Option<Py<PyAny>> {
        let qv = match q.as_slice() {
            Ok(s) => s,
            Err(_) => {
                self.misses += 1;
                return None;
            }
        };
        if self.filled == 0 || qv.len() != self.dims {
            self.misses += 1;
            return None;
        }
        let mut best_i = 0usize;
        let mut best_s = f32::NEG_INFINITY;
        let d = self.dims;
        for e in 0..self.filled {
            let row = &self.sigs[e * d..(e + 1) * d];
            let dot = crate::simd::dot(row, qv);
            if dot > best_s {
                best_s = dot;
                best_i = e;
            }
        }
        if best_s >= self.threshold && self.scopes[best_i] == scope {
            self.hits += 1;
            Python::with_gil(|py| self.results[best_i]
                .as_ref()
                .map(|r| r.clone_ref(py)))
        } else {
            self.misses += 1;
            None
        }
    }

    /// `results` is any Python object (typically a list of (id, score)
    /// tuples); it is cached AS-IS — a later hit hands back the same
    /// object without rebuilding it.
    #[pyo3(signature = (q, results, query, scope=None))]
    fn store(
        &mut self,
        q: PyReadonlyArray1<f32>,
        results: Py<PyAny>,
        query: String,
        scope: Option<String>,
    ) -> PyResult<()> {
        let qv = q.as_slice()?;
        if qv.len() != self.dims {
            return Err(PyValueError::new_err("query length != dims"));
        }
        let e = self.pos;
        self.sigs[e * self.dims..(e + 1) * self.dims].copy_from_slice(qv);
        self.results[e] = Some(results);
        self.queries[e] = Some(query);
        self.scopes[e] = scope;
        self.pos = (self.pos + 1) % self.capacity;
        if self.filled < self.capacity {
            self.filled += 1;
        }
        Ok(())
    }

    fn stats(&self) -> (u64, u64) {
        (self.hits, self.misses)
    }

    fn hit_rate(&self) -> f64 {
        let total = self.hits + self.misses;
        if total == 0 {
            0.0
        } else {
            self.hits as f64 / total as f64
        }
    }

    /// Signatures as nested lists (filled rows × dims) — parity tests.
    fn signatures(&self) -> Vec<Vec<f32>> {
        (0..self.filled)
            .map(|e| self.sigs[e * self.dims..(e + 1) * self.dims].to_vec())
            .collect()
    }
}
