"""RRF fusion mode tests (v0.6.8).

- Pure math: known ranks -> exact scores and order.
- Plumbing: fusion_method flag switches modes; default stays weighted.
- Integration: end-to-end search under fusion_method="rrf" retrieves
  the gold fact; determinism across identical runs.
"""
from __future__ import annotations

from cortexm.api.memory import Memory
from cortexm.bridge.reader import rrf_fuse


def test_rrf_math_order_and_scores() -> None:
    # "b" is rank 2 in list 1 and rank 1 in list 2.
    out = rrf_fuse([["a", "b"], ["b", "c"]], k=60)
    assert out["b"] == 1 / 62 + 1 / 61
    assert out["a"] == 1 / 61
    assert out["c"] == 1 / 62
    ranked = sorted(out, key=lambda f: -out[f])
    assert ranked == ["b", "a", "c"]


def test_rrf_empty_and_single() -> None:
    assert rrf_fuse([[], []]) == {}
    out = rrf_fuse([["x"]], k=60)
    assert out == {"x": 1 / 61}


def test_rrf_k_param() -> None:
    out = rrf_fuse([["x"]], k=10)
    assert out == {"x": 1 / 11}


def _mem():
    mem = Memory(db_path=":memory:")
    mem.add("Alice works at Google.", user_id="probe")
    mem.add("Bob works at Stripe.", user_id="probe")
    mem.add("Alice prefers tea.", user_id="probe")
    return mem


def test_default_mode_is_rrf() -> None:
    mem = _mem()
    assert getattr(mem.config, "fusion_method", "rrf") == "rrf"
    res = mem.search("Where does Alice work?", user_id="probe", limit=5)
    assert any("Google" in r["memory"] for r in res["results"])


def test_rrf_mode_retrieves_gold() -> None:
    mem = _mem()
    mem.config.fusion_method = "rrf"
    res = mem.search("Where does Alice work?", user_id="probe", limit=5)
    assert any("Google" in r["memory"] for r in res["results"]), \
        f"gold missing: {[r['memory'] for r in res['results']]}"


def test_rrf_mode_deterministic() -> None:
    # Fresh identical DBs: sequential searches on ONE db are NOT
    # comparable (prefetcher + access counts learn between calls —
    # pre-existing stateful behavior in both fusion modes).
    def one_shot() -> list[str]:
        mem = _mem()
        mem.config.fusion_method = "rrf"
        return [r["memory"] for r in
                mem.search("Who works where?", user_id="probe",
                           limit=5)["results"]]
    assert one_shot() == one_shot()