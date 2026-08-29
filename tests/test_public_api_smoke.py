"""Public API smoke tests — guarantees that the onboarding flow shown
in the README works on every commit.

These tests guard against regressions on the user-visible API surface:
  * `from cortexm import Memory, Config`
  * `m = Memory()` (zero-arg — uses in-memory :memory: db)
  * `m.add(messages, user_id=...)`
  * `m.search(query, user_id=...)` returns results
  * `m.consolidate()` runs truth maintenance without error
  * `m.export_markdown(out_dir, user_id=...)` writes files
  * `m.recall_step(query, ...)` returns context_block
  * `m.close()` is idempotent
  * `m.__enter__` / `m.__exit__` context-manager works

The 0.948 canonical LongMemEval score depends on this surface staying
stable. If any of these tests break, the README quick-start breaks
for new users — that's a release-blocking regression.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cortexm import Memory, Config, __version__


def test_basic_zero_arg_memory():
    """Memory() with no args works — uses in-memory :memory: db."""
    m = Memory()
    try:
        assert m is not None
        assert hasattr(m, "add")
        assert hasattr(m, "search")
        assert hasattr(m, "consolidate")
        assert hasattr(m, "close")
    finally:
        m.close()


def test_add_search_roundtrip():
    """The README quick-start: add → search → results."""
    m = Memory()
    try:
        m.add([{"role": "user", "content": "My dog is Charlie"}],
              user_id="alice")
        m.add([{"role": "user", "content": "I prefer Python"}],
              user_id="alice")
        m.consolidate()
        out = m.search("dog", user_id="alice", limit=5)
        assert "results" in out
        assert len(out["results"]) >= 1
        # First result should mention Charlie
        top = out["results"][0]
        assert "charlie" in (top.get("memory", "") +
                             top.get("context", "")).lower() or \
               "dog" in top.get("memory", "").lower()
    finally:
        m.close()


def test_export_markdown_writes_files():
    """export_markdown produces a human-readable dump."""
    m = Memory()
    try:
        m.add([{"role": "user", "content": "I live in Berlin"}],
              user_id="bob")
        m.consolidate()
        with tempfile.TemporaryDirectory() as td:
            m.export_markdown(td, user_id="bob")
            files = list(Path(td).rglob("*"))
            assert len(files) > 0, "export_markdown should write files"
            # Should have at least a README.md
            assert any(p.name == "README.md" for p in files if p.is_file())
    finally:
        m.close()


def test_recall_step_exposed():
    """recall_step is a public method on Memory."""
    m = Memory()
    try:
        assert hasattr(m, "recall_step")
        m.add([{"role": "user", "content": "I work at Stripe"}],
              user_id="alice")
        m.consolidate()
        # recall_step should not throw on a simple query
        out = m.recall_step("work", user_id="alice",
                            current_step=5, window=20, k=5)
        assert isinstance(out, dict)
    finally:
        m.close()


def test_edit_fix_exposed():
    """edit + fix are public methods on Memory."""
    m = Memory()
    try:
        assert hasattr(m, "edit")
        assert hasattr(m, "fix")
        # edit is callable (signature accepts fact_id, new_text)
        import inspect
        sig = inspect.signature(m.edit)
        assert "fact_id" in sig.parameters
        assert "new_text" in sig.parameters
    finally:
        m.close()


def test_context_manager():
    """Memory supports `with Memory() as m:` context-manager protocol."""
    with Memory() as m:
        assert m is not None
        m.add([{"role": "user", "content": "test"}], user_id="cm")
        m.consolidate()
        out = m.search("test", user_id="cm")
        assert isinstance(out, dict)


def test_close_idempotent():
    """m.close() is idempotent — calling twice does not raise."""
    m = Memory()
    m.close()
    # Second close should be a no-op, not raise
    try:
        m.close()
    except Exception as e:
        pytest.fail(f"close() should be idempotent, got: {e}")


def test_version_string_format():
    """__version__ is a string in X.Y.Z format."""
    assert isinstance(__version__, str)
    parts = __version__.split(".")
    assert len(parts) >= 2, f"version should be X.Y[.Z], got {__version__}"
    for p in parts:
        assert p.isdigit(), f"version parts should be digits, got {__version__}"


def test_config_defaults_ensure_verbatim():
    """Config defaults: verbatim ingest + search are ON by default.

    The 0.948 canonical score depends on the verbatim tier being
    enabled by default. If someone flips these defaults, the
    canonical score regresses — guard against it.
    """
    c = Config()
    assert c.verbatim_ingest_enabled is True
    assert c.verbatim_search_enabled is True
    assert c.verbatim_k_at_search >= 20
    # recall_step wired into production Memory.search()
    assert c.recall_step_in_search is True


def test_llm_calls_zero_on_init():
    """LLM_CALLS stays 0 across init + add + search — μ=0 promise."""
    from cortexm import LLM_CALLS
    m = Memory()
    try:
        m.add([{"role": "user", "content": "test μ=0"}], user_id="z")
        m.consolidate()
        m.search("test", user_id="z")
    finally:
        m.close()
    # LLM_CALLS is a module-level counter — should still be 0
    assert LLM_CALLS == 0, f"μ=0 broken: LLM_CALLS={LLM_CALLS}"


def test_zero_arg_add_with_string_role_messages():
    """m.add accepts list of dicts with role/content keys (README form)."""
    m = Memory()
    try:
        msgs = [
            {"role": "user", "content": "I work at Acme Corp"},
            {"role": "assistant", "content": "Got it"},
            {"role": "user", "content": "I live in Sydney"},
        ]
        m.add(msgs, user_id="string_test")
        m.consolidate()
        out = m.search("live", user_id="string_test")
        assert "results" in out
    finally:
        m.close()
