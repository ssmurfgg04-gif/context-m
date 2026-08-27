//! Runtime-dispatched SIMD dot product.
//!
//! Auto-vectorization of float reductions is not guaranteed (FP addition
//! is not associative, so LLVM conservatively keeps the loop scalar).
//! Production libraries ship explicit SIMD kernels with runtime CPU
//! feature detection — that is what this module does:
//!
//!   * AVX2 + FMA path: 8 floats/cycle FMADD, ~8x scalar throughput;
//!   * scalar fallback: correct everywhere, baseline speed;
//!   * dispatch cost: one atomic load per call (std caches the result).

#[cfg(target_arch = "x86_64")]
use std::arch::x86_64::*;

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
#[target_feature(enable = "fma")]
unsafe fn dot_avx2(a: &[f32], b: &[f32]) -> f32 {
    let mut acc = _mm256_setzero_ps();
    let n = a.len();
    let mut i = 0;
    while i + 8 <= n {
        let va = _mm256_loadu_ps(a.as_ptr().add(i));
        let vb = _mm256_loadu_ps(b.as_ptr().add(i));
        acc = _mm256_fmadd_ps(va, vb, acc);
        i += 8;
    }
    let mut r = [0f32; 8];
    _mm256_storeu_ps(r.as_mut_ptr(), acc);
    let mut s = r[0] + r[1] + r[2] + r[3] + r[4] + r[5] + r[6] + r[7];
    while i < n {
        s += a[i] * b[i];
        i += 1;
    }
    s
}

fn dot_scalar(a: &[f32], b: &[f32]) -> f32 {
    let mut acc = [0f32; 8];
    let n = a.len();
    let mut i = 0;
    while i + 8 <= n {
        for u in 0..8 {
            acc[u] += a[i + u] * b[i + u];
        }
        i += 8;
    }
    let mut tail = 0f32;
    while i < n {
        tail += a[i] * b[i];
        i += 1;
    }
    (acc[0] + acc[1] + acc[2] + acc[3] + acc[4] + acc[5] + acc[6] + acc[7])
        + tail
}

/// Dispatch to the best available kernel.
pub fn dot(a: &[f32], b: &[f32]) -> f32 {
    debug_assert_eq!(a.len(), b.len());
    #[cfg(target_arch = "x86_64")]
    {
        if std::arch::is_x86_feature_detected!("avx2")
            && std::arch::is_x86_feature_detected!("fma")
        {
            return unsafe { dot_avx2(a, b) };
        }
    }
    dot_scalar(a, b)
}

/// INT8×f32 asymmetric dot (dequantize-on-the-fly), AVX2-dispatched.
pub fn dot_i8_f32(q8: &[i8], q: &[f32]) -> f32 {
    debug_assert_eq!(q8.len(), q.len());
    #[cfg(target_arch = "x86_64")]
    {
        if std::arch::is_x86_feature_detected!("avx2") {
            return unsafe { dot_i8_f32_avx2(q8, q) };
        }
    }
    let mut acc = [0f32; 8];
    let n = q8.len();
    let mut i = 0;
    while i + 8 <= n {
        for u in 0..8 {
            acc[u] += q8[i + u] as f32 * q[i + u];
        }
        i += 8;
    }
    let mut tail = 0f32;
    while i < n {
        tail += q8[i] as f32 * q[i];
        i += 1;
    }
    (acc[0] + acc[1] + acc[2] + acc[3] + acc[4] + acc[5] + acc[6] + acc[7])
        + tail
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
unsafe fn dot_i8_f32_avx2(q8: &[i8], q: &[f32]) -> f32 {
    let n = q8.len();
    let mut acc = _mm256_setzero_ps();
    let mut i = 0;
    while i + 8 <= n {
        // load 8 int8 -> widen to two f32 pairs via cvtepi8_epi32
        let v8 = _mm_loadl_epi64(q8.as_ptr().add(i) as *const __m128i);
        let w = _mm256_cvtepi8_epi32(v8); // 8 x i32
        let wq = _mm256_loadu_ps(q.as_ptr().add(i));
        let wf = _mm256_cvtepi32_ps(w);
        acc = _mm256_fmadd_ps(wf, wq, acc);
        i += 8;
    }
    let mut r = [0f32; 8];
    _mm256_storeu_ps(r.as_mut_ptr(), acc);
    let mut s = r[0] + r[1] + r[2] + r[3] + r[4] + r[5] + r[6] + r[7];
    while i < n {
        s += q8[i] as f32 * q[i];
        i += 1;
    }
    s
}
