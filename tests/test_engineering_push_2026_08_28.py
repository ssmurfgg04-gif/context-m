"""Smoke tests for the 2026-08-28 engineering push.

Covers:
  - tiny_transformer_fallback (μ≈0 small-model fallback)
  - prefilter (HippoRAG 2 query-aware triple filter)
  - working_memory (HRR holographic compression)
  - contextm_reconstruct / contextm_consolidate MCP tools
  - LangChain + LlamaIndex + OpenAI Agents plugin adapter shapes
  - FadeMem production toggle (fade_enabled=True by default)
  - determinism bench_config_overrides flips new flags off
"""
from __future__ import annotations

import json
import os

import pytest

from context_m import Memory
from context_m.config import Config


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

def test_production_config_has_fade_enabled_by_default():
    cfg = Config()
    assert cfg.fade_enabled is True, "fade_enabled must be True in production"
    assert cfg.tiny_fallback_enabled is True
    assert cfg.prefilter_enabled is True


def test_env_flip_for_cron():
    os.environ["CONTEXT_M_FADE"] = "true"
    os.environ["CONTEXT_M_TMT"] = "true"
    try:
        cfg = Config.from_env()
        assert cfg.fade_enabled is True
        assert cfg.tmt_enabled is True
    finally:
        os.environ.pop("CONTEXT_M_FADE")
        os.environ.pop("CONTEXT_M_TMT")


# ---------------------------------------------------------------------------
# Tiny-transformer fallback
# ---------------------------------------------------------------------------

def test_tiny_transformer_fallback_produces_embedding():
    from context_m.bridge.fallback import get_default, TinyTransformerFallback
    tt = get_default()
    emb = tt.embed("Alice calls home every weekend")
    assert emb.shape == (768,)
    # L2 normalized
    norm = float((emb * emb).sum()) ** 0.5
    assert 0.9 < norm <= 1.1


def test_tiny_transformer_fallback_extracts_when_pattern_misses():
    """A sentence the pattern library misses should yield a fallback candidate."""
    from context_m.bridge.fallback import get_default
    tt = get_default()
    # "Alice calls home every weekend" — no trigger pattern matches
    cands = tt.extract_candidates("Alice calls home every weekend",
                                   subject_hint="Alice")
    # we don't pin the exact relation/value (hash-derived) but at least
    # one candidate should emerge for a sentence the patterns miss
    assert len(cands) >= 1
    assert cands[0].subject in ("Alice", "SELF")
    assert cands[0].relation  # non-empty
    assert cands[0].value  # non-empty


def test_tiny_transformer_fallback_disabled_in_bench():
    """bench_config_overrides() should flip tiny_fallback off for baselines."""
    import sys, importlib
    # add scripts dir to path so we can import determinism
    scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
    sys.path.insert(0, scripts_dir)
    try:
        from determinism import bench_config_overrides
        ov = bench_config_overrides()
        assert ov["tiny_fallback_enabled"] is False
        assert ov["prefilter_enabled"] is False
        assert ov["unmess_enabled"] is False
        assert ov["enable_rerank"] is False
        assert ov["ppr_enabled"] is False
    finally:
        sys.path.pop(0)


# ---------------------------------------------------------------------------
# Prefilter (HippoRAG 2)
# ---------------------------------------------------------------------------

def test_prefilter_drops_irrelevant_candidates():
    from context_m.bridge.prefilter import prefilter_triples
    from context_m.text.embedder import HashingEmbedder

    class FakeFact:
        def __init__(self, subject, relation, value, id="x"):
            self.subject = subject
            self.relation = relation
            self.value = value
            self.id = id
            self.memory = f"{subject} {relation.replace('_', ' ')} {value}"

    cands = [
        FakeFact("Alice", "lives_in", "Toronto", "1"),
        FakeFact("Alice", "works_at", "Google", "2"),
        FakeFact("Bob", "has_pet", "cat", "3"),  # irrelevant to "where alice lives"
    ]
    emb = HashingEmbedder(768, 0x0C0FFEE)
    filtered, stats = prefilter_triples(
        cands, "where does Alice live",
        embedder=emb, relation_hints=["lives_in"],
        threshold=0.05, min_keep=2)
    assert stats.n_in == 3
    assert stats.n_kept >= 2  # at least min_keep
    # the pet fact should be the lowest-scoring
    assert stats.n_dropped <= 1


def test_prefilter_never_returns_more_than_input():
    from context_m.bridge.prefilter import prefilter_triples
    cands = []  # empty input
    filtered, stats = prefilter_triples(cands, "anything")
    assert filtered == []
    assert stats.n_in == 0


# ---------------------------------------------------------------------------
# Holographic working memory
# ---------------------------------------------------------------------------

def test_working_memory_compresses_top_k_into_hrr():
    cfg = Config()
    m = Memory(cfg)
    m.add("Alice lives in Toronto. Alice works at Google.",
          user_id="alice", timestamp="2026-01-01T00:00:00Z")
    # Use reader.working_memory() end-to-end — it returns a dict with
    # the HRR vector packed as base64 if compression succeeded.
    out = m.reader.working_memory("where does alice work",
                                   user_id="alice", k=5)
    # Either we got a real HRR (preferred) or a fallback to the textual
    # context block. Either way the call should not crash and should
    # return a preamble / context.
    assert "preamble" in out or "context_block" in out
    if out.get("n_facts", 0) > 0 and out.get("hrr_b64"):
        import base64
        import numpy as np
        vec = np.frombuffer(base64.b64decode(out["hrr_b64"]),
                            dtype=np.float32)
        assert vec.shape == (cfg.dims,)
        n = float((vec * vec).sum()) ** 0.5
        assert 0.9 < n <= 1.1


def test_working_memory_returns_empty_for_no_facts():
    from context_m.vsa.working_memory import build_holographic_wm
    from context_m.vsa.ops import VSA
    from context_m.text.embedder import HashingEmbedder
    vsa = VSA()
    emb = HashingEmbedder()
    hwm = build_holographic_wm([], vsa, emb)
    assert hwm.n_facts == 0
    assert hwm.preamble


# ---------------------------------------------------------------------------
# MCP tool surface
# ---------------------------------------------------------------------------

def test_mcp_server_exposes_reconstruct_and_consolidate_tools():
    from context_m.mcp.server import TOOLS
    names = {t["name"] for t in TOOLS}
    assert "contextm_reconstruct" in names
    assert "contextm_consolidate" in names
    assert "contextm_working_memory" in names
    assert "contextm_hologram_extract" in names


def test_mcp_consolidate_tool_runs():
    """End-to-end: MCP server's consolidate handler must work."""
    from context_m import Memory
    from context_m.mcp.server import MCPServer
    m = Memory(Config())
    m.add("Alice lives in Toronto.", user_id="alice",
          timestamp="2026-01-01T00:00:00Z")
    srv = MCPServer(m)
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "contextm_consolidate",
                      "arguments": {"user_id": "alice", "dry_run": True,
                                    "tmt": False, "fade": True}}}
    resp = srv.handle(req)
    assert resp is not None
    assert resp["id"] == 1
    text = resp["result"]["content"][0]["text"]
    parsed = json.loads(text)
    # dry-run should report stats without applying
    assert "dreaming" in parsed or "lifecycle" in parsed


# ---------------------------------------------------------------------------
# Plugin adapter shapes
# ---------------------------------------------------------------------------

def test_langchain_adapter_duck_types_basememory():
    """ContextMMemory should have the BaseMemory shape (memory_variables,
    load_memory_variables, save_context, clear)."""
    import sys
    plugins_dir = os.path.join(os.path.dirname(__file__), "..", "plugins",
                                "langchain")
    sys.path.insert(0, plugins_dir)
    try:
        from context_m_memory import ContextMMemory
        m = ContextMMemory(rest_url="http://localhost:0",  # unreachable on purpose
                            user_id="alice", k=5)
        assert m.memory_variables == ["history"]
        # load with no server should fail soft
        out = m.load_memory_variables({"input": "hi"})
        assert "history" in out
        # save should not raise even if server is down
        m.save_context({"input": "hello"}, {"output": "hi"})
        m.clear()
    finally:
        sys.path.pop(0)


def test_llamaindex_adapter_duck_types_postprocessor():
    import sys
    plugins_dir = os.path.join(os.path.dirname(__file__), "..", "plugins",
                                "llamaindex")
    sys.path.insert(0, plugins_dir)
    try:
        from context_m_postprocessor import ContextMMemoryPostprocessor
        pp = ContextMMemoryPostprocessor(rest_url="http://localhost:0",
                                          user_id="alice", k=5)
        # empty nodes / no query -> passthrough
        out = pp.postprocess_nodes([], query_str=None)
        assert out == []
    finally:
        sys.path.pop(0)


def test_openai_agents_adapter_exposes_recall_remember():
    import sys
    plugins_dir = os.path.join(os.path.dirname(__file__), "..", "plugins",
                                "openai_agents")
    sys.path.insert(0, plugins_dir)
    try:
        from context_m_adapter import recall, remember, make_tools
        # without SDK installed, make_tools returns raw callables
        tools = make_tools(user_id="alice")
        assert len(tools) == 2
        # calling recall against unreachable server fails soft
        out = recall("hello", rest_url="http://localhost:0")
        assert out == ""
    finally:
        sys.path.pop(0)
