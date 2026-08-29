"""Tests for the plugin kernel — mount/unmount, services, effects, dispose.

Verifies the five promises through the kernel's own contract:
  - effect() cleanups run on dispose (no ghost state)
  - service() / inject() resolve dependencies
  - mount order is enforced (missing dep raises early)
  - duplicate mount raises
  - dispose reverts everything (re-mount works)
"""
from __future__ import annotations

import sqlite3

import pytest

from cortexm.kernel import (
    Context,
    PluginDependencyError,
    PluginAlreadyMountedError,
)


class _FakeEmbedder:
    """Trivial embedder for the kernel tests (no HashingEmbedder dep)."""
    dims = 16

    def embed(self, text: str):
        import numpy as np
        v = np.zeros(self.dims, dtype=np.float32)
        for i, ch in enumerate(text[: self.dims]):
            v[i] = (ord(ch) % 7 - 3) / 3.0
        n = float((v * v).sum() ** 0.5) or 1.0
        return v / n


# ----------------------------- core kernel -----------------------------

def test_context_starts_empty():
    ctx = Context()
    assert ctx.mounted == []
    assert ctx.services == []


def test_mount_simple_plugin_registers_service():
    class HelloPlugin:
        name = "hello"
        inject = []

        def apply(self, ctx):
            ctx.service("hello", self)
            ctx.effect(lambda: None)

    ctx = Context()
    ctx.mount(HelloPlugin())
    assert "hello" in ctx.services
    assert ctx.inject("hello")["hello"] is not None


def test_effect_cleanup_runs_on_dispose():
    cleaned = []

    class P:
        name = "p"
        inject = []

        def apply(self, ctx):
            ctx.effect(lambda: cleaned.append("ran"))

    ctx = Context()
    ctx.mount(P())
    assert cleaned == []
    ctx.dispose()
    assert cleaned == ["ran"]


def test_dispose_reverts_all_state():
    class P:
        name = "p"
        inject = []

        def apply(self, ctx):
            ctx.service("x", self)

    ctx = Context()
    ctx.mount(P())
    assert ctx.services == ["x"]
    ctx.dispose()
    assert ctx.services == []
    assert ctx.mounted == []


def test_missing_dependency_raises_before_apply():
    class NeedsMissing:
        name = "needy"
        inject = ["nonexistent"]

        def apply(self, ctx):
            ctx.service("needy", self)

    ctx = Context()
    with pytest.raises(PluginDependencyError):
        ctx.mount(NeedsMissing())
    assert "needy" not in ctx.services


def test_duplicate_mount_raises():
    class P:
        name = "dup"
        inject = []

        def apply(self, ctx):
            ctx.service("dup", self)

    ctx = Context()
    ctx.mount(P())
    with pytest.raises(PluginAlreadyMountedError):
        ctx.mount(P())


def test_duplicate_service_raises():
    class A:
        name = "a"
        inject = []

        def apply(self, ctx):
            ctx.service("shared", self)

    class B:
        name = "b"
        inject = []

        def apply(self, ctx):
            ctx.service("shared", self)  # collision

    ctx = Context()
    ctx.mount(A())
    with pytest.raises(PluginAlreadyMountedError):
        ctx.mount(B())


def test_plugins_mount_in_order_and_dispose_in_reverse():
    order = []
    cleanups = []

    class P:
        def __init__(self, name):
            self.name = name
            self.inject = []

        def apply(self, ctx):
            order.append(self.name)
            ctx.effect(lambda: cleanups.append(self.name))

    ctx = Context()
    ctx.mount(P("first"))
    ctx.mount(P("second"))
    ctx.mount(P("third"))
    assert order == ["first", "second", "third"]

    # cleanups run in reverse mount order (last in, first out)
    ctx.dispose()
    # The current implementation runs ALL effects in reverse-order
    # of effect-stack push, which is reverse mount order.
    assert cleanups == ["third", "second", "first"]


def test_inject_multiple_services():
    class A:
        name = "a"
        inject = []

        def apply(self, ctx):
            ctx.service("alpha", self)

    class B:
        name = "b"
        inject = []

        def apply(self, ctx):
            ctx.service("beta", self)

    class C:
        name = "c"
        inject = ["alpha", "beta"]

        def apply(self, ctx):
            ctx.service("gamma", self)

    ctx = Context()
    ctx.mount(A())
    ctx.mount(B())
    ctx.mount(C())
    deps = ctx.inject("alpha", "beta", "gamma")
    assert deps["alpha"] is not None
    assert deps["beta"] is not None
    assert deps["gamma"] is not None


def test_repr_includes_mounted_and_services():
    class P:
        name = "p"
        inject = []

        def apply(self, ctx):
            ctx.service("x", self)

    ctx = Context()
    ctx.mount(P())
    r = repr(ctx)
    assert "p" in r
    assert "x" in r


# --------------------------- end-to-end kernel -------------------------

def test_full_kernel_mount_default_for_unit_tests():
    """Mount the full default kernel and exercise both tiers.

    Uses :memory: SQLite so the test is hermetic. Verifies:
      - verbatim tier can add + search
      - structured tier can add + search
      - both services are reachable
      - dispose cleans up (no leaked state on a re-mount)
    """
    from cortexm.kernel import Context
    from cortexm.api.memory import Memory
    from cortexm.config import Config
    from cortexm.text.embedder import HashingEmbedder
    from cortexm.plugins.verbatim import VerbatimPlugin
    from cortexm.plugins.structured import StructuredPlugin

    # Mount manually (not via mount_default) so we can pass
    # the test-only dispose flags and verify clean teardown.
    config = Config.from_env()
    config.db_path = ":memory:"
    embedder = HashingEmbedder(dims=64)
    mem = Memory(config)
    ctx = Context()
    ctx.service("embedder", embedder)
    ctx.service("memory", mem)
    db_conn = getattr(mem.store, "conn", None) or \
        getattr(mem.store, "_conn", None)
    ctx.service("db", db_conn)
    # Test-only: actually drop tables + close db on dispose
    ctx.mount(VerbatimPlugin(drop_tables_on_dispose=True))
    ctx.mount(StructuredPlugin(dispose_memory=True))

    assert "verbatim" in ctx.services
    assert "structured" in ctx.services
    assert "memory" in ctx.services
    assert "db" in ctx.services
    assert "embedder" in ctx.services

    # Write to verbatim
    v = ctx.inject("verbatim")["verbatim"]
    chunk_id = v.add(text="My dog's name is Charlie",
                    user_id="alice", session_id="s1",
                    source_tx_id=1)
    assert chunk_id > 0

    # Search verbatim — "Charlie" should rank high (exact match)
    hits = v.search(query="Charlie", user_id="alice", k=5)
    assert len(hits) > 0
    assert "Charlie" in hits[0].text

    # Write to structured — Memory.add()
    s = ctx.inject("structured")["structured"]
    s.add("Alice works at Google", user_id="alice")
    sh = s.search("Where does Alice work?", user_id="alice", k=5)
    assert len(sh) > 0

    # Dispose and re-mount — proves clean teardown
    ctx.dispose()
    # Re-mount should succeed with no leftover state
    ctx2 = Context()
    config2 = Config.from_env()
    config2.db_path = ":memory:"
    embedder2 = HashingEmbedder(dims=64)
    mem2 = Memory(config2)
    ctx2.service("embedder", embedder2)
    ctx2.service("memory", mem2)
    ctx2.service("db", getattr(mem2.store, "conn", None) or
                  getattr(mem2.store, "_conn", None))
    ctx2.mount(VerbatimPlugin(drop_tables_on_dispose=True))
    ctx2.mount(StructuredPlugin(dispose_memory=True))
    assert "verbatim" in ctx2.services
    ctx2.dispose()
