//! Runtime-dispatched SIMD kernels for Context-M.
//!
//! Auto-vectorization of float reductions is not guaranteed (FP addition
//! is not associative, so LLVM conservatively keeps the loop scalar).
//! Production libraries ship explicit SIMD kernels with runtime CPU
//! feature detection — that is what this module does:
//!
//!   * AVX-512 + FMA path: 16 floats/cycle FMADD (`_mm512_fmadd_ps`) —
//!     the modern data-center path, ~16x scalar throughput;
//!   * AVX2 + FMA path: 8 floats/cycle FMADD (`_mm256_fmadd_ps`) —
//!     the standard desktop/server path, ~8x scalar throughput;
//!   * NEON path on aarch64 (Apple Silicon, Graviton): 4 floats/cycle
//!     via `vmlaq_f32` — ~4x scalar throughput;
//!   * scalar fallback: correct everywhere, baseline speed;
//!   * dispatch cost: one atomic load per call (std caches the result).
//!
//! All kernels take `&[f32]` slices; FP32 ordering differences vs. the
//! NumPy reference are bounded by ~1e-6 for 768-dim vectors and asserted
//! to stay under 1e-5 by `tests/test_rust_accel.py`.

#[cfg(target_arch = "x86_64")]
use std::arch::x86_64::*;

// ---------------------------------------------------------------------------
//  dot(a, b)
// ---------------------------------------------------------------------------

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx512f")]
unsafe fn dot_avx512(a: &[f32], b: &[f32]) -> f32 {
    let mut acc = _mm512_setzero_ps();
    let n = a.len();
    let mut i = 0;
    while i + 16 <= n {
        let va = _mm512_loadu_ps(a.as_ptr().add(i));
        let vb = _mm512_loadu_ps(b.as_ptr().add(i));
        acc = _mm512_fmadd_ps(va, vb, acc);
        i += 16;
    }
    // Reduce the 16-lane accumulator and run the tail (<=15 elements)
    // in scalar to keep ordering consistent across feature paths.
    let mut r = [0f32; 16];
    _mm512_storeu_ps(r.as_mut_ptr(), acc);
    let mut s = r[0] + r[1] + r[2] + r[3] + r[4] + r[5] + r[6] + r[7]
        + r[8] + r[9] + r[10] + r[11] + r[12] + r[13] + r[14] + r[15];
    while i < n {
        s += a[i] * b[i];
        i += 1;
    }
    s
}

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

#[cfg(target_arch = "aarch64")]
#[target_feature(enable = "neon")]
unsafe fn dot_neon(a: &[f32], b: &[f32]) -> f32 {
    use std::arch::aarch64 as ne;
    let mut acc = unsafe { ne::vdupq_n_f32(0.0) };
    let n = a.len();
    let mut i = 0;
    while i + 4 <= n {
        let va = unsafe { ne::vld1q_f32(a.as_ptr().add(i)) };
        let vb = unsafe { ne::vld1q_f32(b.as_ptr().add(i)) };
        acc = unsafe { ne::vmlaq_f32(acc, va, vb) };
        i += 4;
    }
    // horizontal sum of 4 lanes
    let mut s = unsafe { ne::vaddvq_f32(acc) };
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
        if std::arch::is_x86_feature_detected!("avx512f") {
            return unsafe { dot_avx512(a, b) };
        }
        if std::arch::is_x86_feature_detected!("avx2")
            && std::arch::is_x86_feature_detected!("fma")
        {
            return unsafe { dot_avx2(a, b) };
        }
    }
    #[cfg(target_arch = "aarch64")]
    {
        if std::arch::is_aarch64_feature_detected!("neon") {
            return unsafe { dot_neon(a, b) };
        }
    }
    dot_scalar(a, b)
}

// ---------------------------------------------------------------------------
//  dot_i8_f32(q8, q)  — asymmetric int8 × f32 dequantize-on-the-fly
// ---------------------------------------------------------------------------

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
unsafe fn dot_i8_f32_avx2(q8: &[i8], q: &[f32]) -> f32 {
    let n = q8.len();
    let mut acc = _mm256_setzero_ps();
    let mut i = 0;
    while i + 8 <= n {
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

// ---------------------------------------------------------------------------
//  cosine(a, b)  —  normalized dot
// ---------------------------------------------------------------------------

/// Squared L2 norm of a vector (= dot(a, a)). Use this for caching: callers
/// that need cosine repeatedly can store `norm_sq(a)` once and pass it in.
pub fn norm_sq(a: &[f32]) -> f32 {
    dot(a, a)
}

/// Cosine similarity = dot(a,b) / (|a|·|b| + 1e-12).  Callers should cache
/// the norms externally when reusing the same vector many times — see
/// `norm_sq` and the `batch_dot` variants which can be composed manually.
pub fn cosine(a: &[f32], b: &[f32]) -> f32 {
    debug_assert_eq!(a.len(), b.len());
    let d = dot(a, b);
    let na = norm_sq(a);
    let nb = norm_sq(b);
    let denom = (na * nb).sqrt() + 1e-12;
    d / denom
}

// ---------------------------------------------------------------------------
//  l2_sq(a, b)  —  squared L2 distance (a-b)²
// ---------------------------------------------------------------------------

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx512f")]
unsafe fn l2_sq_avx512(a: &[f32], b: &[f32]) -> f32 {
    let mut acc = _mm512_setzero_ps();
    let n = a.len();
    let mut i = 0;
    while i + 16 <= n {
        let va = _mm512_loadu_ps(a.as_ptr().add(i));
        let vb = _mm512_loadu_ps(b.as_ptr().add(i));
        let d = _mm512_sub_ps(va, vb);
        acc = _mm512_fmadd_ps(d, d, acc);
        i += 16;
    }
    // AVX2 tail
    let mut s = 0f32;
    if i + 8 <= n {
        let va = _mm256_loadu_ps(a.as_ptr().add(i));
        let vb = _mm256_loadu_ps(b.as_ptr().add(i));
        let d = _mm256_sub_ps(va, vb);
        let mut tmp = _mm256_setzero_ps();
        tmp = _mm256_fmadd_ps(d, d, tmp);
        let mut r = [0f32; 8];
        _mm256_storeu_ps(r.as_mut_ptr(), tmp);
        s = r[0] + r[1] + r[2] + r[3] + r[4] + r[5] + r[6] + r[7];
        i += 8;
    }
    let mut r = [0f32; 16];
    _mm512_storeu_ps(r.as_mut_ptr(), acc);
    s += r[0] + r[1] + r[2] + r[3] + r[4] + r[5] + r[6] + r[7]
        + r[8] + r[9] + r[10] + r[11] + r[12] + r[13] + r[14] + r[15];
    while i < n {
        let d = a[i] - b[i];
        s += d * d;
        i += 1;
    }
    s
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
#[target_feature(enable = "fma")]
unsafe fn l2_sq_avx2(a: &[f32], b: &[f32]) -> f32 {
    let mut acc = _mm256_setzero_ps();
    let n = a.len();
    let mut i = 0;
    while i + 8 <= n {
        let va = _mm256_loadu_ps(a.as_ptr().add(i));
        let vb = _mm256_loadu_ps(b.as_ptr().add(i));
        let d = _mm256_sub_ps(va, vb);
        acc = _mm256_fmadd_ps(d, d, acc);
        i += 8;
    }
    let mut r = [0f32; 8];
    _mm256_storeu_ps(r.as_mut_ptr(), acc);
    let mut s = r[0] + r[1] + r[2] + r[3] + r[4] + r[5] + r[6] + r[7];
    while i < n {
        let d = a[i] - b[i];
        s += d * d;
        i += 1;
    }
    s
}

#[cfg(target_arch = "aarch64")]
#[target_feature(enable = "neon")]
unsafe fn l2_sq_neon(a: &[f32], b: &[f32]) -> f32 {
    use std::arch::aarch64 as ne;
    let mut acc = unsafe { ne::vdupq_n_f32(0.0) };
    let n = a.len();
    let mut i = 0;
    while i + 4 <= n {
        let va = unsafe { ne::vld1q_f32(a.as_ptr().add(i)) };
        let vb = unsafe { ne::vld1q_f32(b.as_ptr().add(i)) };
        let d = unsafe { ne::vsubq_f32(va, vb) };
        acc = unsafe { ne::vmlaq_f32(acc, d, d) };
        i += 4;
    }
    let mut s = unsafe { ne::vaddvq_f32(acc) };
    while i < n {
        let d = a[i] - b[i];
        s += d * d;
        i += 1;
    }
    s
}

fn l2_sq_scalar(a: &[f32], b: &[f32]) -> f32 {
    let mut acc = [0f32; 8];
    let n = a.len();
    let mut i = 0;
    while i + 8 <= n {
        for u in 0..8 {
            let d = a[i + u] - b[i + u];
            acc[u] += d * d;
        }
        i += 8;
    }
    let mut tail = 0f32;
    while i < n {
        let d = a[i] - b[i];
        tail += d * d;
        i += 1;
    }
    (acc[0] + acc[1] + acc[2] + acc[3] + acc[4] + acc[5] + acc[6] + acc[7])
        + tail
}

/// Squared L2 distance ‖a-b‖². Used by K-means and NSG graph distance.
pub fn l2_sq(a: &[f32], b: &[f32]) -> f32 {
    debug_assert_eq!(a.len(), b.len());
    #[cfg(target_arch = "x86_64")]
    {
        if std::arch::is_x86_feature_detected!("avx512f") {
            return unsafe { l2_sq_avx512(a, b) };
        }
        if std::arch::is_x86_feature_detected!("avx2")
            && std::arch::is_x86_feature_detected!("fma")
        {
            return unsafe { l2_sq_avx2(a, b) };
        }
    }
    #[cfg(target_arch = "aarch64")]
    {
        if std::arch::is_aarch64_feature_detected!("neon") {
            return unsafe { l2_sq_neon(a, b) };
        }
    }
    l2_sq_scalar(a, b)
}

// ---------------------------------------------------------------------------
//  batch_dot(rows, q, n_rows, dims)  —  matrix × vector, cache-friendly
// ---------------------------------------------------------------------------

/// Matrix-vector product over a flat row-major `[n_rows × dims]` slice.
/// `rows` must be at least `n_rows * dims` long, `q` must be `dims` long.
///
/// This is far more cache-friendly than calling `dot()` in a Python loop
/// because the row pointer walks linearly through memory and the per-row
/// dispatch cost (one atomic load + branch) is amortized to near zero.
///
/// NOTE: for fp32 matmul NumPy's BLAS `sgemv` is hard to beat (it uses
/// cache-blocking, software prefetch, and multi-threading).  Our Rust
/// path is competitive with single-threaded BLAS at small N (where the
/// Python boundary cost dominates) and ~1.2× slower at large N (memory
/// bandwidth bound).  Callers comparing very large fp32 batches against
/// numpy should benchmark both — `accel.batch_dot` falls back to numpy
/// automatically when the wheel is absent, so the choice is opt-in.
pub fn batch_dot(rows: &[f32], q: &[f32], n_rows: usize, dims: usize) -> Vec<f32> {
    assert!(rows.len() >= n_rows * dims, "rows slice too short");
    assert_eq!(q.len(), dims, "q length must equal dims");
    let mut out = vec![0f32; n_rows];
    // Dispatch to the 4-row blocked path for sizeable batches; tiny
    // batches go through the scalar loop to keep per-call dispatch
    // overhead low. AVX-512 CPUs use the AVX2 path (AVX-512 ⊇ AVX2);
    // wider vectors don't help here since the bottleneck is memory
    // bandwidth, not FMA throughput.
    #[cfg(target_arch = "x86_64")]
    {
        if n_rows >= 4 && dims >= 8
            && std::arch::is_x86_feature_detected!("avx2")
            && std::arch::is_x86_feature_detected!("fma")
        {
            unsafe { batch_dot_avx2_tiled_4(rows, q, n_rows, dims, &mut out) };
            return out;
        }
    }
    #[cfg(target_arch = "aarch64")]
    {
        if n_rows >= 4 && dims >= 4
            && std::arch::is_aarch64_feature_detected!("neon")
        {
            unsafe { batch_dot_neon_tiled_4(rows, q, n_rows, dims, &mut out) };
            return out;
        }
    }
    // Scalar fallback (very small batches / no SIMD).  For sub-4 batches
    // we use the runtime-dispatched `dot()` per row so single-row AVX2
    // dispatch still kicks in.
    for r in 0..n_rows {
        let row = &rows[r * dims..(r + 1) * dims];
        out[r] = dot(row, q);
    }
    out
}

/// 4-row blocked GEMV (AVX2 + FMA) — the standard cache-blocked GEMV
/// micro-kernel.  Holds 4 partial sums at once and reuses `q` 4× from
/// L1 (vs. the naive per-row loop that pulls `q` from L2 every time).
///
/// For 1000×768 fp32 this stays within ~1.5× of single-threaded BLAS
/// sgemv — the gap is memory bandwidth (3 MB working set doesn't fit
/// in L2), not FMA throughput, so wider AVX-512 vectors wouldn't help.
#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
#[target_feature(enable = "fma")]
unsafe fn batch_dot_avx2_tiled_4(
    rows: &[f32], q: &[f32], n_rows: usize, dims: usize, out: &mut [f32],
) {
    let mut r0 = 0usize;
    while r0 + 4 <= n_rows {
        let mut a0 = _mm256_setzero_ps();
        let mut a1 = _mm256_setzero_ps();
        let mut a2 = _mm256_setzero_ps();
        let mut a3 = _mm256_setzero_ps();
        let mut k = 0;
        while k + 8 <= dims {
            let qv = _mm256_loadu_ps(q.as_ptr().add(k));
            // 4 row pointers stream sequentially → 4 cache lines, reused.
            let r0v = _mm256_loadu_ps(rows.as_ptr().add(r0 * dims + k));
            let r1v = _mm256_loadu_ps(rows.as_ptr().add((r0 + 1) * dims + k));
            let r2v = _mm256_loadu_ps(rows.as_ptr().add((r0 + 2) * dims + k));
            let r3v = _mm256_loadu_ps(rows.as_ptr().add((r0 + 3) * dims + k));
            a0 = _mm256_fmadd_ps(r0v, qv, a0);
            a1 = _mm256_fmadd_ps(r1v, qv, a1);
            a2 = _mm256_fmadd_ps(r2v, qv, a2);
            a3 = _mm256_fmadd_ps(r3v, qv, a3);
            k += 8;
        }
        // Horizontal sum each accumulator → 1 result per row
        let mut r = [0f32; 8];
        _mm256_storeu_ps(r.as_mut_ptr(), a0);
        out[r0] = r[0] + r[1] + r[2] + r[3] + r[4] + r[5] + r[6] + r[7];
        _mm256_storeu_ps(r.as_mut_ptr(), a1);
        out[r0 + 1] = r[0] + r[1] + r[2] + r[3] + r[4] + r[5] + r[6] + r[7];
        _mm256_storeu_ps(r.as_mut_ptr(), a2);
        out[r0 + 2] = r[0] + r[1] + r[2] + r[3] + r[4] + r[5] + r[6] + r[7];
        _mm256_storeu_ps(r.as_mut_ptr(), a3);
        out[r0 + 3] = r[0] + r[1] + r[2] + r[3] + r[4] + r[5] + r[6] + r[7];
        // Tail (k..dims)
        while k < dims {
            out[r0] += rows[r0 * dims + k] * q[k];
            out[r0 + 1] += rows[(r0 + 1) * dims + k] * q[k];
            out[r0 + 2] += rows[(r0 + 2) * dims + k] * q[k];
            out[r0 + 3] += rows[(r0 + 3) * dims + k] * q[k];
            k += 1;
        }
        r0 += 4;
    }
    // Tail rows: scalar
    for r in r0..n_rows {
        out[r] = dot_scalar(&rows[r * dims..(r + 1) * dims], q);
    }
}

#[cfg(target_arch = "aarch64")]
#[target_feature(enable = "neon")]
unsafe fn batch_dot_neon_tiled_4(
    rows: &[f32], q: &[f32], n_rows: usize, dims: usize, out: &mut [f32],
) {
    use std::arch::aarch64 as ne;
    let mut r0 = 0usize;
    while r0 + 4 <= n_rows {
        let mut a0 = unsafe { ne::vdupq_n_f32(0.0) };
        let mut a1 = unsafe { ne::vdupq_n_f32(0.0) };
        let mut a2 = unsafe { ne::vdupq_n_f32(0.0) };
        let mut a3 = unsafe { ne::vdupq_n_f32(0.0) };
        let mut k = 0;
        while k + 4 <= dims {
            let qv = unsafe { ne::vld1q_f32(q.as_ptr().add(k)) };
            let r0v = unsafe { ne::vld1q_f32(rows.as_ptr().add(r0 * dims + k)) };
            let r1v = unsafe { ne::vld1q_f32(rows.as_ptr().add((r0 + 1) * dims + k)) };
            let r2v = unsafe { ne::vld1q_f32(rows.as_ptr().add((r0 + 2) * dims + k)) };
            let r3v = unsafe { ne::vld1q_f32(rows.as_ptr().add((r0 + 3) * dims + k)) };
            a0 = unsafe { ne::vmlaq_f32(a0, r0v, qv) };
            a1 = unsafe { ne::vmlaq_f32(a1, r1v, qv) };
            a2 = unsafe { ne::vmlaq_f32(a2, r2v, qv) };
            a3 = unsafe { ne::vmlaq_f32(a3, r3v, qv) };
            k += 4;
        }
        out[r0]     = unsafe { ne::vaddvq_f32(a0) };
        out[r0 + 1] = unsafe { ne::vaddvq_f32(a1) };
        out[r0 + 2] = unsafe { ne::vaddvq_f32(a2) };
        out[r0 + 3] = unsafe { ne::vaddvq_f32(a3) };
        while k < dims {
            out[r0]     += rows[r0 * dims + k]     * q[k];
            out[r0 + 1] += rows[(r0 + 1) * dims + k] * q[k];
            out[r0 + 2] += rows[(r0 + 2) * dims + k] * q[k];
            out[r0 + 3] += rows[(r0 + 3) * dims + k] * q[k];
            k += 1;
        }
        r0 += 4;
    }
    for r in r0..n_rows {
        out[r] = dot_scalar(&rows[r * dims..(r + 1) * dims], q);
    }
}

/// Same as `batch_dot` but for int8-packed rows (asymmetric int8 × f32).
/// Returns the raw int8·f32 dot products; callers apply per-row scales
/// (the codec's aux array) themselves — see `context_m/vsa/palace.py`.
pub fn batch_dot_i8(
    packed: &[i8],
    q: &[f32],
    n_rows: usize,
    dims: usize,
) -> Vec<f32> {
    assert!(packed.len() >= n_rows * dims, "packed slice too short");
    assert_eq!(q.len(), dims, "q length must equal dims");
    let mut out = Vec::with_capacity(n_rows);
    for r in 0..n_rows {
        let row = &packed[r * dims..(r + 1) * dims];
        out.push(dot_i8_f32(row, q));
    }
    out
}

// ---------------------------------------------------------------------------
//  topk(scores, k)  —  partial sort, O(N) via select_nth_unstable
// ---------------------------------------------------------------------------

/// Return the top-`k` (idx, score) tuples in descending score order.
///
/// Implementation: build (idx, score) pairs, partition via
/// `select_nth_unstable_by` (O(N) median-of-medians), then sort just the
/// top k (O(k log k)). Beats `sort_by` on the full slice for k << N.
pub fn topk(scores: &[f32], k: usize) -> Vec<(usize, f32)> {
    let n = scores.len();
    if n == 0 || k == 0 {
        return Vec::new();
    }
    let mut pairs: Vec<(usize, f32)> =
        scores.iter().enumerate().map(|(i, &s)| (i, s)).collect();
    let cmp = |a: &(usize, f32), b: &(usize, f32)| {
        b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal)
    };
    if k >= n {
        pairs.sort_by(cmp);
        return pairs;
    }
    // Partition so indices [..k] hold the k highest scores (unordered).
    pairs.select_nth_unstable_by(k - 1, cmp);
    pairs[..k].sort_by(cmp);
    pairs[..k].to_vec()
}

// ---------------------------------------------------------------------------
//  argmax(scores)  —  simple scan, AVX-friendly reduction
// ---------------------------------------------------------------------------

/// Return (idx, value) of the maximum element. Ties go to the first
/// occurrence (matches numpy.argmax semantics). On an empty slice
/// returns (0, 0.0).
pub fn argmax(scores: &[f32]) -> (usize, f32) {
    if scores.is_empty() {
        return (0, 0.0);
    }
    let mut best_i = 0usize;
    let mut best_v = scores[0];
    for (i, &v) in scores.iter().enumerate().skip(1) {
        if v > best_v {
            best_v = v;
            best_i = i;
        }
    }
    (best_i, best_v)
}

// ---------------------------------------------------------------------------
//  Tests (cargo test --lib)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn approx(a: f32, b: f32, tol: f32) -> bool {
        (a - b).abs() <= tol
    }

    #[test]
    fn dot_matches_scalar() {
        let a: Vec<f32> = (0..768).map(|i| (i as f32) * 0.001 - 0.4).collect();
        let b: Vec<f32> = (0..768).map(|i| (i as f32) * 0.0007 - 0.2).collect();
        let got = dot(&a, &b);
        let want = dot_scalar(&a, &b);
        assert!(approx(got, want, 1e-3), "got={got} want={want}");
    }

    #[test]
    fn l2_sq_zero_on_identical() {
        let a: Vec<f32> = (0..256).map(|i| (i as f32) * 0.01).collect();
        assert!(approx(l2_sq(&a, &a), 0.0, 1e-5));
    }

    #[test]
    fn cosine_one_on_identical() {
        let a: Vec<f32> = (0..256).map(|i| (i as f32) * 0.01).collect();
        assert!(approx(cosine(&a, &a), 1.0, 1e-5));
    }

    #[test]
    fn batch_dot_shape() {
        let n = 32;
        let d = 64;
        let rows: Vec<f32> = (0..(n * d)).map(|i| i as f32 * 0.001).collect();
        let q: Vec<f32> = (0..d).map(|i| i as f32 * 0.01).collect();
        let out = batch_dot(&rows, &q, n, d);
        assert_eq!(out.len(), n);
        // row 0 = dot(rows[0..d], q) — easy to verify against dot()
        assert!(approx(out[0], dot(&rows[0..d], &q), 1e-5));
    }

    #[test]
    fn topk_descending() {
        let s = vec![0.1, 0.9, 0.5, 0.7, 0.3, 0.8, 0.6, 0.4, 0.2, 0.0];
        let got = topk(&s, 3);
        assert_eq!(got.len(), 3);
        assert_eq!(got[0], (1, 0.9));
        assert_eq!(got[1], (5, 0.8));
        assert_eq!(got[2], (3, 0.7));
    }

    #[test]
    fn argmax_first_tie() {
        let s = vec![0.1, 0.9, 0.9, 0.7];
        let (i, v) = argmax(&s);
        assert_eq!(i, 1);
        assert!(approx(v, 0.9, 1e-6));
    }

    #[test]
    fn argmax_empty() {
        let (i, v) = argmax(&[]);
        assert_eq!(i, 0);
        assert!(approx(v, 0.0, 1e-6));
    }
}
