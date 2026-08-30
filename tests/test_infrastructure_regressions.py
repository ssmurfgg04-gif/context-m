"""Regression coverage for transaction, provenance, and packaging plumbing."""

import os
from pathlib import Path

import pytest

from cortexm import Memory
from cortexm.config import Config
from cortexm.kernel import Context
from cortexm.plugins.verbatim import VerbatimPlugin
from cortexm.text.embedder import HashingEmbedder


def test_add_batch_rolls_back_when_a_later_item_fails():
    mem = Memory(verbatim_ingest_enabled=True)
    original = mem.writer.add
    calls = 0

    def failing_add(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("forced failure")
        return original(*args, **kwargs)

    mem.writer.add = failing_add
    with pytest.raises(RuntimeError, match="forced failure"):
        mem.add_batch(["My name is Ada", "I work at Acme"], user_id="u")
    assert mem.store.all_chunks("u") == []
    assert mem.store.query_facts(user_id="u") == []
    assert mem.store.conn.execute(
        "SELECT COUNT(*) FROM verbatim_chunks WHERE user_id='u'"
    ).fetchone()[0] == 0


def test_verbatim_preserves_trace_chunk_id():
    mem = Memory()
    mem.add("My dog is named Charlie", user_id="u")
    chunk_id = mem.store.all_chunks("u")[0]["id"]
    source_id = mem.store.conn.execute(
        "SELECT source_tx_id FROM verbatim_chunks WHERE user_id='u'"
    ).fetchone()[0]
    assert source_id == chunk_id


def test_query_expansion_result_has_valid_shape():
    ctx = Context()
    ctx.service("db", __import__("sqlite3").connect(":memory:"))
    ctx.service("embedder", HashingEmbedder())
    ctx.mount(VerbatimPlugin(query_cache_enabled=False))
    plugin = ctx.inject("verbatim")["verbatim"]
    plugin.add(text="Ada works at Acme", user_id="u")
    hit = plugin._bm25_only(query="works", user_id="u")[0]
    assert hit.bm25_norm >= 0
    assert hit.cosine_sim == 0.0


def test_config_validates_environment_overrides(monkeypatch):
    monkeypatch.setenv("CORTEXM_DIMS", "7")
    with pytest.raises(ValueError, match="multiple of 8"):
        Config.from_env()


def test_dockerfile_uses_current_package_name():
    root = Path(__file__).parents[1]
    dockerfile = (root / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY cortexm ./cortexm" in dockerfile
    assert '"-m", "cortexm.server.rest"' in dockerfile
    assert (root / "cortexm" / "server" / "rest.py").is_file()
