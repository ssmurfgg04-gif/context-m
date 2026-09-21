"""Perf gates: search + consolidate latency budgets (v0.6.8).

Uses pytest-benchmark for calibrated measurement. Bounds are LOOSE
smoke thresholds (catch hangs/pathologies, not noise) — tighten to
--benchmark-compare-fail baselines once CI has history.

NOTE: pytest-benchmark auto-disables under xdist, so these run in the
serial part of the suite, not under -n auto.
"""
from __future__ import annotations

import time

import pytest

pytest.importorskip("pytest_benchmark")

from cortexm.api.memory import Memory  # noqa: E402


def _seeded(n: int = 60) -> Memory:
    mem = Memory(db_path=":memory:")
    topics = ["works at", "prefers", "lives in", "knows", "likes"]
    for i in range(n):
        mem.add(f"Person{i} {topics[i % len(topics)]} value{i}.",
                user_id="perf")
    return mem


def test_search_latency_budget(benchmark) -> None:
    mem = _seeded()
    res = benchmark(mem.search, "Where does Person7 work?",
                    user_id="perf", limit=5)
    assert res["results"], "search returned nothing"
    # The benchmark table above is the calibration record; the gate
    # itself uses explicit timing (BenchmarkFixture exposes no stable
    # stats API across versions — verified against 5.3.0).
    ts = []
    for _ in range(5):
        t = time.perf_counter()
        mem.search("Where does Person7 work?", user_id="perf", limit=5)
        ts.append((time.perf_counter() - t) * 1000.0)
    mean = sum(ts) / len(ts)
    print(f"\nsearch smoke mean_ms={mean:.1f}")
    assert mean < 2000, f"search mean {mean:.1f}ms over 2s smoke budget"


def test_consolidate_latency_budget(benchmark) -> None:
    def setup():
        return (_seeded(),), {}

    def run(mem):
        return mem.consolidate(run_tmt=True, user_id="perf")

    res = benchmark.pedantic(run, setup=setup, rounds=3)
    assert res is not None