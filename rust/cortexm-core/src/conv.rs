//! HRR circular-convolution binding (Plate 1995) via FFT.
//!
//! Parity with `context_m.vsa.ops.VSA` (mode="conv"):
//!   bind(role, filler)   = irfft(rfft(filler) * rfft(role_vec))
//!   unbind(role, h)      = irfft(rfft(h) * rfft(involution(role_vec)))
//!
//! role vectors are injected from Python for the same bit-parity reason
//! as the permutations (see perm.rs).

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rustfft::{num_complex::Complex, FftPlanner};
use std::collections::HashMap;

#[pyclass]
pub struct ConvBindings {
    dims: usize,
    roles: HashMap<String, Vec<f32>>,
}

#[pymethods]
impl ConvBindings {
    #[new]
    fn new(dims: usize) -> Self {
        ConvBindings {
            dims,
            roles: HashMap::new(),
        }
    }

    /// Inject the authoritative role vector (Python's cached VSA role_vec).
    fn set_role(&mut self, role: &str, vec: Vec<f32>) -> PyResult<()> {
        if vec.len() != self.dims {
            return Err(PyValueError::new_err("role vector length != dims"));
        }
        self.roles.insert(role.to_string(), vec);
        Ok(())
    }

    fn bind(&self, role: &str, filler: Vec<f32>) -> PyResult<Vec<f32>> {
        let r = self.roles
            .get(role)
            .ok_or_else(|| PyValueError::new_err(format!("role '{role}' not injected")))?;
        Ok(circular_conv(&filler, r))
    }

    fn unbind(&self, role: &str, h: Vec<f32>) -> PyResult<Vec<f32>> {
        let r = self.roles
            .get(role)
            .ok_or_else(|| PyValueError::new_err(format!("role '{role}' not injected")))?;
        // involution: [r0, r_{d-1}, r_{d-2}, ..., r1]
        let mut inv = Vec::with_capacity(self.dims);
        inv.push(r[0]);
        for i in (1..self.dims).rev() {
            inv.push(r[i]);
        }
        Ok(circular_conv(&h, &inv))
    }

    fn cached_roles(&self) -> usize {
        self.roles.len()
    }
}

/// Real circular convolution via complex FFT (size n).
/// For 768-dim vectors this is ~5x cheaper than the O(n^2) direct form.
fn circular_conv(a: &[f32], b: &[f32]) -> Vec<f32> {
    let n = a.len();
    let mut planner = FftPlanner::new();
    let fft = planner.plan_fft_forward(n);
    let ifft = planner.plan_fft_inverse(n);

    let to_c = |v: &[f32]| -> Vec<Complex<f32>> {
        v.iter().map(|&x| Complex::new(x as f32, 0.0)).collect()
    };
    let mut ca = to_c(a);
    let mut cb = to_c(b);
    fft.process(&mut ca);
    fft.process(&mut cb);
    for i in 0..n {
        ca[i] *= cb[i];
    }
    ifft.process(&mut ca);
    let inv_n = 1.0 / n as f32;
    ca.iter().map(|c| c.re * inv_n).collect()
}
