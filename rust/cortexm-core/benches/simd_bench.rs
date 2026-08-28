//! Criterion micro-benchmarks for the SIMD kernels in `cortexm-core`.
//!
//! Build & run (from `rust/cortexm-core`):
//!     cargo bench --bench simd_bench --features=""
//!
//! The benches measure the runtime-dispatched kernels (AVX-512 → AVX2+FMA
//! → NEON → scalar) on realistic inputs (768-dim, 1000-row batches,
//! 10k-score topk).  They are intentionally light on assertions — the
//! correctness contract lives in `tests/test_rust_accel.py`.

use cortexm_core::simd::{
    argmax, batch_dot, batch_dot_i8, cosine, dot, dot_i8_f32, l2_sq, topk,
};
use criterion::{black_box, criterion_group, criterion_main, Criterion};

fn rand_f32(n: usize, seed: u64) -> Vec<f32> {
    // splitmix64 — fast, deterministic, no extra crate
    let mut s = seed;
    (0..n)
        .map(|_| {
            s = s.wrapping_add(0x9E37_79B9_7F4A_7C15);
            let mut z = s;
            z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
            z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
            (z ^ (z >> 31)) as i32 as f32 * (1.0 / (1u64 << 31) as f32) * 2.0 - 1.0
        })
        .collect()
}

fn rand_i8(n: usize, seed: u64) -> Vec<i8> {
    let mut s = seed;
    (0..n)
        .map(|_| {
            s = s.wrapping_add(0x9E37_79B9_7F4A_7C15);
            let mut z = s;
            z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
            z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
            ((z ^ (z >> 31)) & 0xFF) as i8
        })
        .collect()
}

const D: usize = 768;
const N: usize = 1000;
const SCORES: usize = 10_000;

fn bench_dot(c: &mut Criterion) {
    let a = rand_f32(D, 1);
    let b = rand_f32(D, 2);
    c.bench_function("dot_768", |bn| {
        bn.iter(|| {
            let r = dot(black_box(&a), black_box(&b));
            black_box(r);
        })
    });
}

fn bench_dot_i8_f32(c: &mut Criterion) {
    let q8 = rand_i8(D, 3);
    let q = rand_f32(D, 4);
    c.bench_function("dot_i8_f32_768", |bn| {
        bn.iter(|| {
            let r = dot_i8_f32(black_box(&q8), black_box(&q));
            black_box(r);
        })
    });
}

fn bench_cosine(c: &mut Criterion) {
    let a = rand_f32(D, 5);
    let b = rand_f32(D, 6);
    c.bench_function("cosine_768", |bn| {
        bn.iter(|| {
            let r = cosine(black_box(&a), black_box(&b));
            black_box(r);
        })
    });
}

fn bench_l2_sq(c: &mut Criterion) {
    let a = rand_f32(D, 7);
    let b = rand_f32(D, 8);
    c.bench_function("l2_sq_768", |bn| {
        bn.iter(|| {
            let r = l2_sq(black_box(&a), black_box(&b));
            black_box(r);
        })
    });
}

fn bench_batch_dot(c: &mut Criterion) {
    let rows = rand_f32(N * D, 9);
    let q = rand_f32(D, 10);
    c.bench_function("batch_dot_1000x768", |bn| {
        bn.iter(|| {
            let r = batch_dot(black_box(&rows), black_box(&q), N, D);
            black_box(r);
        })
    });
}

fn bench_batch_dot_i8(c: &mut Criterion) {
    let packed = rand_i8(N * D, 11);
    let q = rand_f32(D, 12);
    c.bench_function("batch_dot_i8_1000x768", |bn| {
        bn.iter(|| {
            let r = batch_dot_i8(black_box(&packed), black_box(&q), N, D);
            black_box(r);
        })
    });
}

fn bench_topk(c: &mut Criterion) {
    let scores = rand_f32(SCORES, 13);
    c.bench_function("topk_10k_k10", |bn| {
        bn.iter(|| {
            let r = topk(black_box(&scores), 10);
            black_box(r);
        })
    });
}

fn bench_argmax(c: &mut Criterion) {
    let scores = rand_f32(SCORES, 14);
    c.bench_function("argmax_10k", |bn| {
        bn.iter(|| {
            let r = argmax(black_box(&scores));
            black_box(r);
        })
    });
}

criterion_group!(
    benches,
    bench_dot,
    bench_dot_i8_f32,
    bench_cosine,
    bench_l2_sq,
    bench_batch_dot,
    bench_batch_dot_i8,
    bench_topk,
    bench_argmax,
);
criterion_main!(benches);
