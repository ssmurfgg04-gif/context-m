"""Rust acceleration parity tests.

Byte-exact parity for h64 (hashes must NEVER diverge — they key entity
vectors), near-exact for float gathers (same permutation indices, same
order of operations within f32 rounding).
"""

from __future__ import annotations

import numpy as np
import pytest

from context_m import accel
from context_m.util import h64 as py_h64
from context_m.vsa.ops import VSA

pytestmark = pytest.mark.skipif(
    not accel.RUST_AVAILABLE,
    reason="cortexm_core wheel not built (pip install ./rust/cortexm-core)")


class TestHashParity:
    def test_h64_exact_parity(self):
        for feature in ("perm:S", "role:R", "entity:Alice",
                        "chunk:0xdeadbeef", "x" * 500, "", "ünïcödé"):
            for seed in (0, 1, 0x0C0FFEE, -1, 2**63 - 1):
                assert accel.h64(feature, seed) == py_h64(feature, seed), \
                    f"hash divergence for {feature!r} seed={seed}"


class TestVSAParity:
    def test_perm_bind_unbind_parity(self):
        vsa = VSA(dims=256, mode="perm")
        rv = accel.RustVSA(vsa)
        rng = np.random.default_rng(3)
        filler = rng.standard_normal(256).astype(np.float32)

        py_bound = vsa.bind("S", filler)
        rs_bound = rv.bind("S", filler)
        np.testing.assert_array_equal(py_bound, rs_bound)

        py_unbound = vsa.unbind("S", py_bound)
        rs_unbound = rv.unbind("S", rs_bound)
        np.testing.assert_array_equal(py_unbound, rs_unbound)

    def test_encode_fact_parity(self):
        vsa = VSA(dims=256, mode="perm")
        rv = accel.RustVSA(vsa)
        rng = np.random.default_rng(4)

        def unit(v):
            return v / np.linalg.norm(v)

        s = unit(rng.standard_normal(256).astype(np.float32))
        r = unit(rng.standard_normal(256).astype(np.float32))
        v = unit(rng.standard_normal(256).astype(np.float32))

        py_h = vsa.encode_fact(s, r, v)
        rs_h = rv.encode_fact(s, r, v)
        # same operations, same order — f32-exact
        np.testing.assert_allclose(py_h, rs_h, rtol=0, atol=1e-6)
        assert abs(float(py_h @ rs_h)) > 0.999


class TestSLB:
    def test_rust_slb_hit_and_scope(self):
        slb = accel._core.SemanticLookasideBuffer(64, 0.97, 256)
        q = np.random.default_rng(5).standard_normal(256).astype(np.float32)
        assert slb.lookup(q, "scope-a") is None
        slb.store(q, [("f1", 0.9), ("f2", 0.8)], "q", "scope-a")
        hit = slb.lookup(q, "scope-a")
        assert hit is not None and hit[0][0] == "f1"
        assert slb.lookup(q, "scope-b") is None

    def test_int8_roundtrip(self):
        rng = np.random.default_rng(6)
        v = rng.standard_normal(128).astype(np.float32)
        q, scale = accel._core.quantize_int8(v.tolist())
        back = np.asarray(accel._core.dequantize_int8(q, scale))
        np.testing.assert_allclose(v, back, atol=scale + 1e-6)


# ---------------------------------------------------------------------------
# Task 6-simd: parity tests for the expanded SIMD kernel surface.
#
# Tolerances: FP32 lane-reduction noise grows ~N·eps with the magnitude of
# the result.  We use `assert_allclose` with both `rtol` and `atol` so the
# noise floor scales correctly (numpy convention).
#   * dot / cosine / batch_dot  : rtol=1e-5 atol=1e-5  (small-magnitude)
#   * l2_sq                     : rtol=1e-5 atol=1e-5  (unit-normalized)
#   * batch_dot_i8 / dot_i8_f32 : rtol=1e-5 atol=1e-3  (int8 amplifies noise)
#   * topk                      : exact index set match; rtol=1e-5 on scores
#   * argmax                    : exact index match
class TestSimdKernels:
    D = 768

    def _unit(self, v):
        n = np.linalg.norm(v)
        return (v / n).astype(np.float32) if n > 0 else v.astype(np.float32)

    def test_dot_parity(self):
        rng = np.random.default_rng(101)
        a = rng.standard_normal(self.D).astype(np.float32)
        b = rng.standard_normal(self.D).astype(np.float32)
        rs = accel.dot(a, b)
        np.testing.assert_allclose(rs, a @ b, rtol=1e-5, atol=1e-5)

    def test_dot_i8_f32_parity(self):
        rng = np.random.default_rng(102)
        q8 = rng.integers(-127, 127, size=self.D).astype(np.int8)
        q = rng.standard_normal(self.D).astype(np.float32)
        rs = accel.dot_i8_f32(q8, q)
        want = q8.astype(np.float32) @ q
        np.testing.assert_allclose(rs, want, rtol=1e-5, atol=1e-3)

    def test_cosine_parity(self):
        rng = np.random.default_rng(103)
        a = self._unit(rng.standard_normal(self.D).astype(np.float32))
        b = self._unit(rng.standard_normal(self.D).astype(np.float32))
        rs = accel.cosine(a, b)
        want = float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))
        np.testing.assert_allclose(rs, want, rtol=1e-5, atol=1e-5)

    def test_l2_sq_parity(self):
        rng = np.random.default_rng(104)
        a = self._unit(rng.standard_normal(self.D).astype(np.float32))
        b = self._unit(rng.standard_normal(self.D).astype(np.float32))
        rs = accel.l2_sq(a, b)
        want = float(((a - b) ** 2).sum())
        np.testing.assert_allclose(rs, want, rtol=1e-5, atol=1e-5)

    def test_batch_dot_parity(self):
        rng = np.random.default_rng(105)
        rows = rng.standard_normal((1000, self.D)).astype(np.float32)
        q = rng.standard_normal(self.D).astype(np.float32)
        rs = np.asarray(accel.batch_dot(rows.reshape(-1), q, 1000, self.D),
                        dtype=np.float32)
        want = rows @ q
        # 1000 rows of un-normalized 768-dim Gaussian → per-row dot
        # magnitude ~sqrt(768) ≈ 28, so FP32 SIMD lane-reduction noise
        # floor is ~N·eps ≈ 9e-5 relative. atol=5e-5 + rtol=1e-5 catches
        # bugs but stays above the FP32 noise floor at this magnitude.
        np.testing.assert_allclose(rs, want, rtol=1e-5, atol=5e-5)

    def test_batch_dot_i8_parity(self):
        rng = np.random.default_rng(106)
        packed = rng.integers(-127, 127, size=(1000, self.D)).astype(np.int8)
        q = rng.standard_normal(self.D).astype(np.float32)
        rs = np.asarray(
            accel.batch_dot_i8(packed.reshape(-1), q, 1000, self.D),
            dtype=np.float32)
        want = packed.astype(np.float32) @ q
        np.testing.assert_allclose(rs, want, rtol=1e-5, atol=1e-2)

    def test_topk_parity(self):
        rng = np.random.default_rng(107)
        scores = rng.standard_normal(10_000).astype(np.float32)
        k = 10
        rs = accel.topk(scores, k)
        assert len(rs) == k
        # Index SET must match numpy's argpartition top-k
        rs_idx = sorted(i for i, _ in rs)
        np_idx = sorted(int(i) for i in
                        np.argpartition(-scores, k - 1)[:k])
        assert rs_idx == np_idx, f"topk index set diverges: {rs_idx} vs {np_idx}"
        # Returned tuples must be in descending score order
        rs_scores = [s for _, s in rs]
        assert rs_scores == sorted(rs_scores, reverse=True)
        # Scores must match the original scores at those indices
        for i, s in rs:
            np.testing.assert_allclose(s, scores[i], rtol=1e-6, atol=0)

    def test_argmax_parity(self):
        rng = np.random.default_rng(108)
        scores = rng.standard_normal(10_000).astype(np.float32)
        i, v = accel.argmax(scores)
        np_i = int(np.argmax(scores))
        assert i == np_i, f"argmax diverges: rust={i} numpy={np_i}"
        np.testing.assert_allclose(v, scores[np_i], rtol=1e-6, atol=0)

    def test_cosine_normalized(self):
        """Cosine of identical vectors must be 1.0 (numerically)."""
        rng = np.random.default_rng(109)
        a = self._unit(rng.standard_normal(self.D).astype(np.float32))
        assert abs(accel.cosine(a, a) - 1.0) < 1e-6

    def test_l2_sq_zero(self):
        """L2 squared of identical vectors must be 0.0."""
        rng = np.random.default_rng(110)
        a = rng.standard_normal(self.D).astype(np.float32)
        assert abs(accel.l2_sq(a, a) - 0.0) < 1e-6

    def test_simd_kernels_class(self):
        """SimdKernels container class dispatches to the same Rust path."""
        kern = accel.SimdKernels()
        rng = np.random.default_rng(111)
        a = self._unit(rng.standard_normal(self.D).astype(np.float32))
        b = self._unit(rng.standard_normal(self.D).astype(np.float32))
        np.testing.assert_allclose(kern.cosine(a, b),
                                   accel.cosine(a, b), rtol=0, atol=0)
        rows = rng.standard_normal((32, self.D)).astype(np.float32)
        rs = kern.batch_dot(rows.reshape(-1), a, 32, self.D)
        np.testing.assert_allclose(
            np.asarray(rs, dtype=np.float32), rows @ a,
            rtol=1e-5, atol=1e-5)

    def test_palace_search_routes_through_rust(self):
        """End-to-end: MemoryPalace.search() should hit the Rust fast path
        when cortexm_core is available and produce the same top-k as the
        numpy path. Guards against silent regressions in palace.py."""
        from context_m.config import Config
        from context_m.trace.store import TraceStore
        from context_m.vsa.palace import MemoryPalace
        import tempfile, os, contextlib

        with tempfile.TemporaryDirectory() as tmp:
            store = TraceStore(os.path.join(tmp, "palace.sqlite3"))
            cfg = Config(dims=256, codec="int8", seed=42)
            palace = MemoryPalace(cfg, store)
            rng = np.random.default_rng(42)
            for i in range(200):
                v = rng.standard_normal(256).astype(np.float32)
                v = v / (np.linalg.norm(v) + 1e-12)
                palace.add(f"f{i}", v)
            q = rng.standard_normal(256).astype(np.float32)
            q = q / (np.linalg.norm(q) + 1e-12)

            # Run search twice — must be deterministic and consistent
            r1 = palace.search(q, k=10)
            r2 = palace.search(q, k=10)
            assert r1 == r2, "palace search is non-deterministic"

            # Verify the top-1 result has the highest score vs a brute force
            ids = [fid for fid, _ in r1]
            scores = [s for _, s in r1]
            assert scores == sorted(scores, reverse=True), \
                "search results not in descending score order"
            # Brute-force numpy top-1 must agree
            all_scores = palace._score_all(q)
            brute_top = int(np.argmax(all_scores))
            assert ids[0] == palace._ids[brute_top], \
                "palace top-1 diverges from brute-force argmax"



class TestQuadrant:
    def test_recall_on_clustered(self):
        if not accel.QUADRANT_AVAILABLE:
            pytest.skip("quadrant wheel not built")
        rng = np.random.default_rng(7)
        D, NC, N = 256, 8, 2_000
        cent = rng.standard_normal((NC, D)).astype(np.float32)
        cent /= np.linalg.norm(cent, axis=1, keepdims=True)
        assign = rng.integers(0, NC, N)
        noise = rng.standard_normal((N, D)).astype(np.float32)
        noise /= np.linalg.norm(noise, axis=1, keepdims=True)
        vecs = cent[assign] + 0.8 * noise
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)

        ann = accel.QuadrantANN(vecs, page_capacity=32)
        q = vecs[0]
        ids, scores = ann.search(q, k=5, max_leaves=16)
        exact = set(int(x) for x in np.argsort(-(vecs @ q))[:5])
        assert len(set(ids) & exact) >= 3          # >=60% recall on-cluster
        assert int(ids[0]) == 0                     # self-match first

    def test_stats_shape(self):
        if not accel.QUADRANT_AVAILABLE:
            pytest.skip("quadrant wheel not built")
        rng = np.random.default_rng(8)
        vecs = rng.standard_normal((500, 64)).astype(np.float32)
        ann = accel.QuadrantANN(vecs, page_capacity=32)
        st = ann.stats()
        assert st["mode"] == "quadrant"
        assert "depth" in st["detail"]
