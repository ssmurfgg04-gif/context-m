//! Permutation binding — the hot path of the default VSA mode.
//!
//! DESIGN NOTE (why perms are injected, not generated here): Python's
//! `VSA.perm()` derives permutations from NumPy's `default_rng(PCG64 +
//! SeedSequence)`. Reproducing SeedSequence bit-for-bit is possible but
//! brittle; a single bit of divergence silently corrupts every hologram.
//! So the *authoritative* permutation lives in Python and is injected
//! once (`set_perm`); Rust accelerates the per-fact GATHER work —
//! bind/unbind bundles, fused fact encoding, normalization — which is
//! where the interpreter overhead actually accumulates.

use numpy::IntoPyArray;
use numpy::PyArray1;
use numpy::PyReadonlyArray1;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::collections::HashMap;

#[pyclass]
pub struct PermBindings {
    dims: usize,
    perms: HashMap<String, Vec<u32>>,
    inperms: HashMap<String, Vec<u32>>,
}

#[pymethods]
impl PermBindings {
    #[new]
    #[pyo3(signature = (dims))]
    fn new(dims: usize) -> Self {
        PermBindings {
            dims,
            perms: HashMap::new(),
            inperms: HashMap::new(),
        }
    }

    /// Inject the authoritative permutation for a role (from Python's
    /// cached VSA perms — identical indices, so holograms stay
    /// bit-compatible across the Python and Rust paths).
    fn set_perm(&mut self, role: &str, perm: Vec<u32>) -> PyResult<()> {
        if perm.len() != self.dims {
            return Err(PyValueError::new_err("perm length != dims"));
        }
        let mut inv = vec![0u32; self.dims];
        for (i, &pi) in perm.iter().enumerate() {
            inv[pi as usize] = i as u32;
        }
        self.perms.insert(role.to_string(), perm);
        self.inperms.insert(role.to_string(), inv);
        Ok(())
    }

    /// bind(role, filler) = filler[perm[role]]
    fn bind<'py>(
        &self,
        py: Python<'py>,
        role: &str,
        filler: PyReadonlyArray1<f32>,
    ) -> PyResult<Bound<'py, PyArray1<f32>>> {
        let p = self
            .perms
            .get(role)
            .ok_or_else(|| PyValueError::new_err(format!("perm for role '{role}' not injected")))?;
        let f = filler.as_slice()?;
        if f.len() != self.dims {
            return Err(PyValueError::new_err("filler length != dims"));
        }
        Ok(permute(p, f).into_pyarray(py))
    }

    /// unbind(role, h) = h[inv_perm[role]]
    fn unbind<'py>(
        &self,
        py: Python<'py>,
        role: &str,
        h: PyReadonlyArray1<f32>,
    ) -> PyResult<Bound<'py, PyArray1<f32>>> {
        let inv = self
            .inperms
            .get(role)
            .ok_or_else(|| PyValueError::new_err(format!("perm for role '{role}' not injected")))?;
        let hv = h.as_slice()?;
        if hv.len() != self.dims {
            return Err(PyValueError::new_err("h length != dims"));
        }
        Ok(permute(inv, hv).into_pyarray(py))
    }

    /// Fused fact encoding — one boundary crossing for the whole
    /// pipeline that `VSA.encode_fact` runs per fact:
    ///   lex  = normalize(s + r + v)
    ///   h    = normalize(bind(S,s) + bind(R,r) + bind(V,v))
    ///   out  = normalize(h + lam * lex)
    #[pyo3(signature = (s_vec, r_vec, v_vec, lam = 0.6))]
    fn encode_fact<'py>(
        &self,
        py: Python<'py>,
        s_vec: PyReadonlyArray1<f32>,
        r_vec: PyReadonlyArray1<f32>,
        v_vec: PyReadonlyArray1<f32>,
        lam: f32,
    ) -> PyResult<Bound<'py, PyArray1<f32>>> {
        let s = s_vec.as_slice()?;
        let r = r_vec.as_slice()?;
        let v = v_vec.as_slice()?;
        if s.len() != self.dims
            || r.len() != self.dims
            || v.len() != self.dims
        {
            return Err(PyValueError::new_err("vector length != dims"));
        }
        let ps = self
            .perms
            .get("S")
            .ok_or_else(|| PyValueError::new_err("perm for role 'S' not injected"))?;
        let pr = self
            .perms
            .get("R")
            .ok_or_else(|| PyValueError::new_err("perm for role 'R' not injected"))?;
        let pv = self
            .perms
            .get("V")
            .ok_or_else(|| PyValueError::new_err("perm for role 'V' not injected"))?;

        let bs = permute(ps, s);
        let br = permute(pr, r);
        let bv = permute(pv, v);

        let mut bound = vec![0f32; self.dims];
        let mut lex = vec![0f32; self.dims];
        let mut n_bound = 0f32;
        let mut n_lex = 0f32;
        for i in 0..self.dims {
            bound[i] = bs[i] + br[i] + bv[i];
            lex[i] = s[i] + r[i] + v[i];
            n_bound += bound[i] * bound[i];
            n_lex += lex[i] * lex[i];
        }
        n_bound = n_bound.sqrt().max(1e-9);
        n_lex = n_lex.sqrt().max(1e-9);

        let mut out = vec![0f32; self.dims];
        let mut n_out = 0f32;
        for i in 0..self.dims {
            out[i] = bound[i] / n_bound + lam * lex[i] / n_lex;
            n_out += out[i] * out[i];
        }
        n_out = n_out.sqrt();
        if n_out > 0.0 {
            for x in out.iter_mut() {
                *x /= n_out;
            }
        }
        Ok(out.into_pyarray(py))
    }

    fn cached_roles(&self) -> usize {
        self.perms.len()
    }
}

#[inline]
fn permute(p: &[u32], v: &[f32]) -> Vec<f32> {
    let mut out = Vec::with_capacity(v.len());
    for &pi in p {
        out.push(v[pi as usize]);
    }
    out
}
