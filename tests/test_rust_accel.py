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
