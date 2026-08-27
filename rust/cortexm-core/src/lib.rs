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

use pyo3::prelude::*;

mod conv;
mod hash;
mod perm;
mod simd;
mod slb;

use crate::conv::ConvBindings;
use crate::hash::h64;
use crate::perm::PermBindings;
use crate::slb::SemanticLookasideBuffer;

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

#[pymodule]
fn cortexm_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(h64, m)?)?;
    m.add_function(wrap_pyfunction!(quantize_int8, m)?)?;
    m.add_function(wrap_pyfunction!(dequantize_int8, m)?)?;
    m.add_function(wrap_pyfunction!(int8_scores, m)?)?;
    m.add_class::<PermBindings>()?;
    m.add_class::<ConvBindings>()?;
    m.add_class::<SemanticLookasideBuffer>()?;
    Ok(())
}
