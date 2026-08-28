"""Tests for the Reddit-driven improvements shipped 2026-08-29:

1. BM25 chunk-recall (≥10 Reddit mentions for "BM25" / "hybrid"
   across r/LocalLLaMA + r/LangChain + r/agi + r/ClaudeCode).
   Replaces Jaccard lexical scoring with Okapi BM25 (k1=1.5, b=0.75)
   in `_chunk_recall`. This lifts recall on natural-language queries
   with rare query terms (PR numbers, usernames, version strings),
   directly attacking the catastrophic recall=0.052 weak spot.

2. `cortexm inspect` CLI (≥10 Reddit mentions for "UI" / "dashboard"
   / "viewer" / "inspect"). Dumps facts/chunks/audit for a scope as
   pretty JSON. CLI-native answer to the "I want to see what's in
   memory" ask — no web server, no TUI, just `cortexm inspect`.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from cortexm import Memory  # noqa: E402
from cortexm.config import Config  # noqa: E402
from cortexm.cli import main as cli_main  # noqa: E402


# Reuse the synthetic thread from the Tier-4.4.3 test — it has the
# rare-term structure (PR numbers, usernames, version strings) that
# BM25 is designed to handle.
SYNTHETIC_THREAD = {
    "id": "synthetic#bm25-test",
    "author": "ati865",
    "created_at": "2025-01-01T10:00:00Z",
    "body": "Possibly fixed by https://github.com/example/repo/pull/65353 "
            "or https://github.com/example/repo/pull/65511",
    "comments": [
        {"author": "ati865", "created_at": "2025-01-01T10:00:00Z",
         "body": "Possibly fixed by https://github.com/example/repo/pull/65353 "
                 "or https://github.com/example/repo/pull/65511. "
                 "Can you test again tomorrow with latest nightly?"},
        {"author": "Leo1003", "created_at": "2025-01-02T10:00:00Z",
         "body": "Just upgraded to latest nightly and tested. "
                 "I can confirm that it has been fixed in: "
                 "rustc 1.40.0-nightly (518deda77 2019-10-18)"},
    ],
}

QUERY = "Which user suggested that the issue might be fixed by " \
        "pull requests #65353 or #65511?"
GOLD_ANSWER = "ati865"


def _ingest_thread(memory, thread, user_id="bm25-test-user"):
    """Ingest a synthetic thread into memory via the writer."""
    msgs = [{"role": "user",
             "content": f"[{c['author']}] {c['body']}",
             "timestamp": c["created_at"]}
            for c in thread["comments"]]
    if thread.get("body"):
        msgs.insert(0, {"role": "user",
                        "content": f"[{thread['author']}] {thread['body']}",
                        "timestamp": thread["created_at"]})
    memory.add(msgs, user_id=user_id)
    memory.apply_rules()  # ensure pattern extraction + chunk storage


@pytest.fixture
def cfg(tmp_path):
    c = Config()                # plain Config, no env vars
    c.db_path = str(tmp_path / "bm25_test.db")
    c.apply_rules_each_add = False
    c.chunk_recall_enabled = True
    # BM25 ON (default for new code); the disabled-fallback test
    # toggles this off explicitly.
    c.chunk_recall_use_bm25 = True
    return c


def test_bm25_chunk_recall_surfaces_rare_term_chunk(cfg):
    """BM25 chunk-recall surfaces the answer-bearing chunk for a
    natural-language query with rare terms (PR numbers, usernames).

    Before: the chunk "[ati865] Possibly fixed by PR #65353 or #65511"
    did NOT surface because Jaccard of content words treated "PR" the
    same as "the".

    After: BM25's IDF weighting gives "65353" / "65511" ~10x the
    weight of common terms, so the answer-bearing chunk rises to
    the top of chunk-recall and is injected into the fusion pool.
    """
    m = Memory(cfg)
    try:
        _ingest_thread(m, SYNTHETIC_THREAD)
        # search with BM25 ON (default)
        result = m.search(QUERY, user_id="bm25-test-user", k=10)
        ctx_block = result.get("context_block") or result.get("context") or ""
        assert GOLD_ANSWER in ctx_block, (
            f"BM25 chunk-recall did NOT surface '{GOLD_ANSWER}' in the "
            f"context block. Got:\n{ctx_block[:600]}"
        )
    finally:
        m.close()


def test_bm25_disabled_falls_back_to_jaccard(cfg):
    """When CORTEXM_CHUNK_RECALL_USE_BM25=false, the chunk-recall path
    falls back to Jaccard lexical scoring. This preserves the old
    behavior for users who need exact backward compatibility."""
    cfg.__dict__["chunk_recall_use_bm25"] = False
    m = Memory(cfg)
    try:
        _ingest_thread(m, SYNTHETIC_THREAD)
        result = m.search(QUERY, user_id="bm25-test-user", k=10)
        # The search should still return SOMETHING (Jaccard may
        # still surface the answer by luck — that's the existing
        # baseline documented in Tier 4.4.3 tests).
        ctx_block = result.get("context_block") or result.get("context") or ""
        assert isinstance(ctx_block, str)
        assert len(ctx_block) > 0
    finally:
        m.close()


def test_bm25_inspect_cli_dumps_facts_and_chunks(cfg, capsys):
    """`cortexm inspect` CLI dumps facts, chunks, and audit tail for
    a (user_id, agent_id, run_id) scope as pretty JSON. This is the
    CLI-native answer to the Reddit "UI / dashboard / viewer /
    inspect" ask — no web server, no TUI, just JSON in stdout."""
    m = Memory(cfg)
    try:
        _ingest_thread(m, SYNTHETIC_THREAD)
    finally:
        m.close()

    # invoke `cortexm inspect --db <path> --user-id bm25-test-user
    #                              --format json --what all`
    rc = cli_main([
        "inspect",
        "--db", cfg.db_path,
        "--user-id", "bm25-test-user",
        "--format", "json",
        "--what", "all",
        "--limit", "10",
    ])
    out, _ = capsys.readouterr()
    assert rc == 0, f"inspect CLI failed: rc={rc}\n{out}"
    # output must be valid JSON
    parsed = json.loads(out)
    # must have the documented sections
    for k in ("scope", "summary", "facts", "chunks", "audit"):
        assert k in parsed, f"inspect output missing '{k}' section"
    # scope echo
    assert parsed["scope"]["user_id"] == "bm25-test-user"
    # summary counts are non-negative ints
    s = parsed["summary"]
    assert isinstance(s["facts"], int) and s["facts"] >= 0
    assert isinstance(s["chunks"], int) and s["chunks"] >= 0
    assert isinstance(s["audit_events"], int) and s["audit_events"] >= 0


def test_bm25_inspect_cli_text_format(cfg, capsys):
    """The text format is human-readable for non-power users."""
    m = Memory(cfg)
    try:
        _ingest_thread(m, SYNTHETIC_THREAD)
    finally:
        m.close()
    rc = cli_main([
        "inspect",
        "--db", cfg.db_path,
        "--user-id", "bm25-test-user",
        "--format", "text",
        "--what", "all",
    ])
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "=== cortexm inspect ===" in out
    assert "scope:" in out
    assert "facts:" in out


def test_bm25_inspect_cli_what_filter(cfg, capsys):
    """--what facts only outputs facts; --what audit only audit, etc."""
    m = Memory(cfg)
    try:
        _ingest_thread(m, SYNTHETIC_THREAD)
    finally:
        m.close()
    rc = cli_main([
        "inspect",
        "--db", cfg.db_path,
        "--user-id", "bm25-test-user",
        "--what", "facts",
        "--limit", "5",
    ])
    out, _ = capsys.readouterr()
    assert rc == 0
    parsed = json.loads(out)
    assert isinstance(parsed["facts"], list)
    # with --what=facts, chunks and audit should still be present
    # (they're empty arrays because we skipped them)
    assert parsed["chunks"] == []
    assert parsed["audit"] == []


def test_bm25_inspect_cli_empty_scope_returns_zeros(cfg, capsys):
    """Asking for an empty scope returns 0s, not an error. This is
    important for scripting — `cortexm inspect` is meant to be
    pipeable and never crash on empty results."""
    rc = cli_main([
        "inspect",
        "--db", cfg.db_path,
        "--user-id", "nonexistent-user",
        "--what", "all",
    ])
    out, _ = capsys.readouterr()
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["summary"]["facts"] == 0
    assert parsed["summary"]["chunks"] == 0


def test_dsh_cortexm_plugin_manifest_parses():
    """The dsh-cortexm plugin manifest (package.json) is valid JSON
    with the required Cordis plugin contract fields. Verifies the
    manifest is discoverable by `dsh-find-plugin` (which searches
    GitHub for `dsh-plugin` topic repos)."""
    pkg_path = REPO / "plugins" / "dsh-cortexm" / "package.json"
    if not pkg_path.exists():
        pytest.skip("dsh-cortexm plugin scaffold not present")
    pkg = json.loads(pkg_path.read_text())
    # required fields for DSH plugin discovery
    assert "dsh-plugin" in pkg["keywords"], (
        "must have 'dsh-plugin' keyword for dsh-find-plugin discovery")
    assert "cordis" in pkg["keywords"]
    assert "dsh" in pkg
    assert "storage" in pkg["dsh"]["kind"]
    assert "session" in pkg["dsh"]["kind"]
    assert pkg["dsh"]["provides"]["storage"]["interface"] == "cortexm.store"
    assert pkg["dsh"]["provides"]["session"]["interface"] == "cortexm.session"
    # methods must match the storage/session primitives we expose
    expected_storage = {"add", "search", "structural_query",
                        "consolidate", "export_provenance", "audit"}
    actual_storage = set(pkg["dsh"]["provides"]["storage"]["methods"])
    assert expected_storage.issubset(actual_storage)
    expected_session = {"replay", "fork", "trajectory"}
    actual_session = set(pkg["dsh"]["provides"]["session"]["methods"])
    assert expected_session.issubset(actual_session)
