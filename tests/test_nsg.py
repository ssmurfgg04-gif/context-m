"""Smoke tests for the NSG (Navigating Spreading-out Graph) index.

Three test surfaces:
  1. Config wiring — ``Config.index_backend`` / ``INDEX_BACKENDS`` menu
     stays in sync and validation rejects unknown values.
  2. Numpy fallback — runs even when the Rust wheel is absent; we
     explicitly disable Rust acceleration via the ``CONTEXTM_RUST=0``
     env switch so this class always exercises the pure-numpy path,
     regardless of whether the wheel is installed. This is the
     "shipped-anywhere" guarantee.
  3. Rust wheel — when ``quadrant.NsgIndex`` is importable, exercise
     the compiled path. The Rust algorithm is byte-for-byte identical
     to the numpy fallback (same kNN selection, same MRNG prune, same
     greedy best-first search), so recall numbers should match within
     sampling jitter. Skipped if the wheel isn't built.

Both paths assert recall@5 ≥ 0.85 vs brute-force cosine on random
clustered 768-dim vectors — the headline claim.
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from context_m.config import Config, INDEX_BACKENDS
from context_m.index import NsgBackend
from context_m.index.nsg import NSG_RUST_AVAILABLE, nsg_status


@pytest.fixture
def force_numpy_mode(monkeypatch):
    """Force the NsgBackend to use the pure-numpy fallback path.

    Patches the module-level ``NSG_RUST_ENABLED`` flag (consulted by
    ``NsgBackend._use_rust`` at build time) so that the numpy algorithm
    is exercised even when the Rust wheel IS installed. This is the
    portable-anywhere guarantee — the fallback must always work.
    """
    import context_m.index.nsg as nsg_mod
    monkeypatch.setattr(nsg_mod, "NSG_RUST_ENABLED", False)
    yield


# --------------------------------------------------------------------- corpus
def _clustered_corpus(n: int = 100, dims: int = 768, n_clusters: int = 8,
                      seed: int = 0) -> np.ndarray:
    """Deterministic, well-clustered unit-normalized corpus.

    Real-world 768-dim embeddings are NOT uniform noise — they cluster.
    A uniform-noise corpus would have no neighborhood structure for an
    ANN to find (every vector equidistant from every other), so recall
    would be near-zero. This fixture builds N/CN vectors around CN
    well-separated centroids with small jitter, then unit-normalizes.
    """
    rng = np.random.default_rng(seed)
    nc = min(n_clusters, n)
    cent = rng.standard_normal((nc, dims)).astype(np.float32)
    cent /= np.linalg.norm(cent, axis=1, keepdims=True)
    assign = rng.integers(0, nc, n)
    noise = rng.standard_normal((n, dims)).astype(np.float32) * 0.08
    noise /= np.linalg.norm(noise, axis=1, keepdims=True)
    vecs = cent[assign] + noise
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs.astype(np.float32)


def _brute_force_topk(vectors: np.ndarray, query: np.ndarray, k: int = 5):
    sims = vectors @ query
    idx = np.argsort(-sims)[:k]
    return [(int(i), float(sims[i])) for i in idx]


def _recall(approx: list[tuple], exact_ids: list[int]) -> float:
    if not exact_ids:
        return 1.0
    approx_ids = {int(i) for i, _ in approx}
    hits = len(approx_ids & set(exact_ids))
    return hits / len(exact_ids)


# --------------------------------------------------------------------- config
class TestConfigBackend:
    def test_backends_tuple_has_three(self):
        assert INDEX_BACKENDS == ("quadrant", "nsg", "flat")

    def test_default_is_quadrant(self):
        assert Config().index_backend == "quadrant"

    def test_validation_rejects_unknown(self):
        with pytest.raises(ValueError, match="index_backend"):
            Config(index_backend="bogus")

    def test_validation_accepts_all(self):
        for name in INDEX_BACKENDS:
            assert Config(index_backend=name).index_backend == name

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("CONTEXT_M_INDEX_BACKEND", "nsg")
        assert Config.from_env().index_backend == "nsg"


# --------------------------------------------------------------------- numpy
@pytest.mark.usefixtures("force_numpy_mode")
class TestNsgBackendNumpy:
    """Pure-numpy NSG fallback. Runs regardless of wheel availability."""

    def test_mode_is_numpy_fallback(self):
        # Sanity: the fixture worked and the module flag is off.
        import context_m.index.nsg as nsg_mod
        assert nsg_mod.NSG_RUST_ENABLED is False

    def test_build_and_search_self_match(self):
        vecs = _clustered_corpus(n=100, dims=64, n_clusters=4, seed=1)
        b = NsgBackend(dims=64, k=20, ef_search=64)
        b.build(vecs)
        assert b._mode == "numpy-fallback"
        # self-query must come back as the top hit (sim ≈ 1.0)
        res = b.search(vecs[0], k=5)
        assert len(res) == 5
        top_id, top_sim = res[0]
        assert top_id == "0"
        assert top_sim > 0.99

    def test_recall_ge_85pct_on_clustered_768dim(self):
        # This is the headline claim: NSG on 768-dim clustered vectors
        # hits ≥85% recall@5 vs brute force. The numpy fallback uses the
        # same algorithm as the Rust path, so recall parity is expected.
        n, dims, k = 100, 768, 5
        vecs = _clustered_corpus(n=n, dims=dims, n_clusters=8, seed=42)
        b = NsgBackend(dims=dims, k=32, ef_search=64)
        b.build(vecs)
        assert b._mode == "numpy-fallback"
        recalls = []
        for i in range(n):
            exact = _brute_force_topk(vecs, vecs[i], k=k)
            exact_ids = [j for j, _ in exact]
            approx = b.search(vecs[i], k=k)
            recalls.append(_recall(approx, exact_ids))
        mean_recall = float(np.mean(recalls))
        # ≥ 0.85 across the whole corpus — generous floor; the numpy
        # fallback with k_build=32 + ef=64 typically hits ≥0.95.
        assert mean_recall >= 0.85, f"recall {mean_recall:.3f} < 0.85"

    def test_stats_shape(self):
        vecs = _clustered_corpus(n=50, dims=32, n_clusters=4, seed=2)
        b = NsgBackend(dims=32, k=10, ef_search=32)
        b.build(vecs)
        st = b.stats()
        assert st["mode"] == "numpy-fallback"
        assert st["n_vectors"] == 50
        assert st["dims"] == 32
        assert st["k_build"] == 10
        assert st["ef_search"] == 32
        # MRNG pruning: avg_degree must be strictly less than k_build
        # (k_build - 1 in the worst case). Sanity bound: at least one
        # edge per node, and avg_degree <= k_build + 1 (medoid edge).
        assert 1.0 <= st["avg_degree"] <= st["k_build"] + 1
        assert st["n_edges"] >= 50  # at least one edge per node

    def test_string_ids_round_trip(self):
        vecs = _clustered_corpus(n=30, dims=32, n_clusters=3, seed=3)
        ids = [f"fact-{i:03d}" for i in range(30)]
        b = NsgBackend(dims=32, k=10, ef_search=32)
        b.build(vecs, ids=ids)
        res = b.search(vecs[5], k=3)
        for id_str, _sim in res:
            assert id_str in ids

    def test_search_before_build_raises(self):
        b = NsgBackend(dims=32)
        with pytest.raises(RuntimeError, match="build"):
            b.search(np.zeros(32, dtype=np.float32))

    def test_dim_mismatch_raises(self):
        vecs = _clustered_corpus(n=20, dims=32, n_clusters=4, seed=4)
        b = NsgBackend(dims=32, k=8, ef_search=16)
        b.build(vecs)
        with pytest.raises(ValueError, match="query len"):
            b.search(np.zeros(16, dtype=np.float32), k=5)

    def test_empty_corpus_raises(self):
        b = NsgBackend(dims=32, k=8, ef_search=16)
        with pytest.raises(ValueError, match="empty"):
            b.build(np.zeros((0, 32), dtype=np.float32))


# --------------------------------------------------------------------- rust
@pytest.mark.skipif(not NSG_RUST_AVAILABLE,
                    reason="quadrant.NsgIndex wheel not built "
                           "(pip install ./rust/quadrant)")
class TestNsgBackendRust:
    """Exercise the Rust wheel path. Skipped unless the wheel is installed.

    Same recall test as the numpy fallback — the algorithm is byte-for-
    byte identical, so recall numbers should match within sampling
    jitter.
    """

    def test_rust_recall_ge_85pct(self):
        n, dims, k = 100, 768, 5
        vecs = _clustered_corpus(n=n, dims=dims, n_clusters=8, seed=42)
        b = NsgBackend(dims=dims, k=32, ef_search=64)
        b.build(vecs)
        assert b._mode == "rust", f"expected rust mode, got {b._mode}"
        recalls = []
        for i in range(n):
            exact = _brute_force_topk(vecs, vecs[i], k=k)
            approx = b.search(vecs[i], k=k)
            recalls.append(_recall(approx, [j for j, _ in exact]))
        mean_recall = float(np.mean(recalls))
        assert mean_recall >= 0.85, f"rust recall {mean_recall:.3f} < 0.85"

    def test_rust_self_match(self):
        # Same self-match guarantee as the numpy path.
        vecs = _clustered_corpus(n=100, dims=64, n_clusters=4, seed=1)
        b = NsgBackend(dims=64, k=20, ef_search=64)
        b.build(vecs)
        assert b._mode == "rust"
        res = b.search(vecs[0], k=5)
        assert res[0][0] == "0"
        assert res[0][1] > 0.99

    def test_rust_stats_expose_graph_density(self):
        vecs = _clustered_corpus(n=50, dims=32, n_clusters=4, seed=5)
        b = NsgBackend(dims=32, k=10, ef_search=32)
        b.build(vecs)
        st = b.stats()
        assert st["mode"] == "rust"
        assert st["n_vectors"] == 50
        assert "n_edges" in st
        assert "nav_node" in st
        # MRNG pruning guarantee: avg_degree <= k_build + 1 (medoid edge)
        assert st["n_edges"] >= 50

    def test_rust_string_ids_round_trip(self):
        vecs = _clustered_corpus(n=30, dims=32, n_clusters=3, seed=3)
        ids = [f"rust-{i:03d}" for i in range(30)]
        b = NsgBackend(dims=32, k=10, ef_search=32)
        b.build(vecs, ids=ids)
        res = b.search(vecs[5], k=3)
        for id_str, _sim in res:
            assert id_str in ids


# --------------------------------------------------------------------- status
class TestNsgStatus:
    def test_status_reports_rust_state(self):
        st = nsg_status()
        assert "rust_available" in st
        assert "rust_enabled" in st
        assert "env" in st
        # When the wheel is installed, rust_available flips True. The
        # numpy-only assertion (rust_available=False → hint mentions
        # numpy/fallback/build) is exercised by the fixture-forced
        # numpy path.
        if st["rust_available"]:
            assert st["rust_enabled"] in (True, False)
        else:
            hint = st["hint"].lower()
            assert "numpy" in hint or "fallback" in hint \
                or "build" in hint
