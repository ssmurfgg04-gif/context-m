//! cortexm-core — accelerated hot paths for Context-M.
//!
//! Scope (mirrors `context_m/util.py::h64`, `context_m/vsa/ops.py`,
//! `context_m/vsa/slb.py`; byte-exact parity is asserted by
//! `tests/test_rust_accel.py` and `benchmarks/rust_vs_numpy.py`):
//!
//! * `h64`            — keyed BLAKE2b 64-bit feature hash
//! * `PermBindings`   — permutation bind/unbind with cached perms
//! * `ConvBindings`   — HRR circular-convolution bind/unbind (FFT)
//! * `SemanticLookasideBuffer` — 64-entry L1-resident query cache
//! * int8 quantize / dequantize / asymmetric dot scores
//!
//! The Python implementation remains the reference; this crate is an
//! accelerator used opportunistically (`context_m.accel`).

use numpy::PyReadonlyArray1;
use pyo3::prelude::*;

mod conv;
mod hash;
mod perm;
pub mod simd;
mod slb;

use crate::conv::ConvBindings;
use crate::hash::h64;
use crate::perm::PermBindings;
use crate::slb::SemanticLookasideBuffer;

// SIMD kernels re-exported under the module top level for `accel.py`
// thin wrappers. `Vec<f32>` and `(usize, f32)` are converted to Python
// list / tuple automatically by pyo3 — no numpy round-trip needed.
use crate::simd::{
    argmax as simd_argmax, batch_dot as simd_batch_dot,
    batch_dot_i8 as simd_batch_dot_i8, cosine as simd_cosine,
    dot as simd_dot, dot_i8_f32 as simd_dot_i8_f32, l2_sq as simd_l2_sq,
    topk as simd_topk,
};

/// Quantize a float32 vector to int8 with a symmetric per-vector scale.
#[pyfunction]
#[pyo3(signature = (vec, scale_bits = 7))]
fn quantize_int8(vec: Vec<f32>, scale_bits: i32) -> (Vec<i8>, f32) {
    let mut maxabs: f32 = 0.0;
    for &x in &vec {
        let a = x.abs();
        if a > maxabs {
            maxabs = a;
        }
    }
    if maxabs <= 0.0 {
        return (vec.iter().map(|_| 0i8).collect(), 1.0);
    }
    let maxval = (1i32 << scale_bits) as f32 - 1.0;
    let scale = maxabs / maxval;
    let inv = maxval / maxabs;
    let q = vec
        .iter()
        .map(|&x| (x * inv).round().clamp(-128.0, 127.0) as i8)
        .collect();
    (q, scale)
}

/// Dequantize int8 with a per-vector scale.
#[pyfunction]
fn dequantize_int8(q: Vec<i8>, scale: f32) -> Vec<f32> {
    q.iter().map(|&x| x as f32 * scale).collect()
}

/// Asymmetric score: int8-packed rows vs float32 query.
/// Returns dot products scaled by 1/dims (comparable to cosine for
/// normalized inputs).
#[pyfunction]
fn int8_scores(packed: Vec<i8>, query: Vec<f32>) -> PyResult<Vec<f32>> {
    if packed.len() % query.len() != 0 {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "packed length must be a multiple of query length",
        ));
    }
    let d = query.len();
    let mut out = Vec::with_capacity(packed.len() / d);
    for row in packed.chunks(d) {
        let mut acc = 0f32;
        for i in 0..d {
            acc += row[i] as f32 * query[i];
        }
        out.push(acc / d as f32);
    }
    Ok(out)
}

// ----------------------------------------------------------- SIMD kernels
// Thin pyo3 wrappers exposing `simd::dot`, `cosine`, `l2_sq`,
// `batch_dot`, `batch_dot_i8`, `topk`, `argmax` to Python. The kernels
// themselves live in `simd.rs` and are runtime-dispatched (AVX-512 →
// AVX2+FMA → NEON → scalar) — these wrappers just pay the Python→Rust
// boundary cost once per call, never per element.
//
// INPUT FORM: all kernels take `PyReadonlyArray1<T>` (zero-copy numpy
// slice view) rather than `Vec<T>`. `accel.py` normalizes inputs via
// `np.ascontiguousarray(x, dtype=...)` before invoking, so callers can
// always pass numpy arrays (or anything that `np.asarray` accepts).
// For batch sizes >1k elements the per-element extraction cost of
// `Vec<T>` would dominate the actual SIMD work, hence the choice.

/// SIMD dot product of two equal-length f32 vectors.
#[pyfunction]
fn dot(a: PyReadonlyArray1<f32>, b: PyReadonlyArray1<f32>) -> PyResult<f32> {
    let a = a.as_slice()?;
    let b = b.as_slice()?;
    if a.len() != b.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "dot: a and b must have equal length",
        ));
    }
    Ok(simd_dot(a, b))
}

/// Asymmetric INT8 × f32 dot product (dequantize-on-the-fly).
#[pyfunction]
fn dot_i8_f32(q8: PyReadonlyArray1<i8>, q: PyReadonlyArray1<f32>) -> PyResult<f32> {
    let q8 = q8.as_slice()?;
    let q = q.as_slice()?;
    if q8.len() != q.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "dot_i8_f32: q8 and q must have equal length",
        ));
    }
    Ok(simd_dot_i8_f32(q8, q))
}

/// Cosine similarity = dot(a,b) / (|a|·|b| + 1e-12).
#[pyfunction]
fn cosine(a: PyReadonlyArray1<f32>, b: PyReadonlyArray1<f32>) -> PyResult<f32> {
    let a = a.as_slice()?;
    let b = b.as_slice()?;
    if a.len() != b.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "cosine: a and b must have equal length",
        ));
    }
    Ok(simd_cosine(a, b))
}

/// Squared L2 distance ‖a-b‖².
#[pyfunction]
fn l2_sq(a: PyReadonlyArray1<f32>, b: PyReadonlyArray1<f32>) -> PyResult<f32> {
    let a = a.as_slice()?;
    let b = b.as_slice()?;
    if a.len() != b.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "l2_sq: a and b must have equal length",
        ));
    }
    Ok(simd_l2_sq(a, b))
}

/// Matrix-vector product over a flat `[n_rows × dims]` f32 row-major
/// slice. Far more cache-friendly than calling `dot()` per row from
/// Python — one boundary crossing for the whole batch.
#[pyfunction]
fn batch_dot(
    rows: PyReadonlyArray1<f32>,
    q: PyReadonlyArray1<f32>,
    n_rows: usize,
    dims: usize,
) -> PyResult<Vec<f32>> {
    let rows = rows.as_slice()?;
    let q = q.as_slice()?;
    if rows.len() < n_rows * dims {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "batch_dot: rows slice too short",
        ));
    }
    if q.len() != dims {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "batch_dot: q length must equal dims",
        ));
    }
    Ok(simd_batch_dot(rows, q, n_rows, dims))
}

/// Same as `batch_dot` but for int8-packed rows (asymmetric int8 × f32).
/// Returns raw int8·f32 dot products; callers apply per-row scales.
#[pyfunction]
fn batch_dot_i8(
    packed: PyReadonlyArray1<i8>,
    q: PyReadonlyArray1<f32>,
    n_rows: usize,
    dims: usize,
) -> PyResult<Vec<f32>> {
    let packed = packed.as_slice()?;
    let q = q.as_slice()?;
    if packed.len() < n_rows * dims {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "batch_dot_i8: packed slice too short",
        ));
    }
    if q.len() != dims {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "batch_dot_i8: q length must equal dims",
        ));
    }
    Ok(simd_batch_dot_i8(packed, q, n_rows, dims))
}

/// Return the top-`k` (idx, score) tuples in descending score order.
/// O(N) via `select_nth_unstable`, then O(k log k) for the prefix sort.
#[pyfunction]
fn topk(scores: PyReadonlyArray1<f32>, k: usize) -> Vec<(usize, f32)> {
    // as_slice can only fail on a non-contiguous array; accel.py always
    // normalizes inputs to contiguous, but be defensive by handling both.
    match scores.as_slice() {
        Ok(s) => simd_topk(s, k),
        Err(_) => {
            let s = scores.as_array().to_owned();
            simd_topk(s.as_slice().unwrap_or(&[]), k)
        }
    }
}

/// Return (idx, value) of the maximum element (ties → first occurrence,
/// matching `numpy.argmax`).
#[pyfunction]
fn argmax(scores: PyReadonlyArray1<f32>) -> (usize, f32) {
    match scores.as_slice() {
        Ok(s) => simd_argmax(s),
        Err(_) => (0, 0.0),
    }
}

#[pymodule]
fn cortexm_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(h64, m)?)?;
    m.add_function(wrap_pyfunction!(quantize_int8, m)?)?;
    m.add_function(wrap_pyfunction!(dequantize_int8, m)?)?;
    m.add_function(wrap_pyfunction!(int8_scores, m)?)?;
    m.add_function(wrap_pyfunction!(dot, m)?)?;
    m.add_function(wrap_pyfunction!(dot_i8_f32, m)?)?;
    m.add_function(wrap_pyfunction!(cosine, m)?)?;
    m.add_function(wrap_pyfunction!(l2_sq, m)?)?;
    m.add_function(wrap_pyfunction!(batch_dot, m)?)?;
    m.add_function(wrap_pyfunction!(batch_dot_i8, m)?)?;
    m.add_function(wrap_pyfunction!(topk, m)?)?;
    m.add_function(wrap_pyfunction!(argmax, m)?)?;
    m.add_class::<PermBindings>()?;
    m.add_class::<ConvBindings>()?;
    m.add_class::<SemanticLookasideBuffer>()?;
    Ok(())
}
