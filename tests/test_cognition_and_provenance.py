"""Smoke tests for the HMS Cognition Engine port + Provenance stack.

Covers:
  - PatternScanner surfaces regularities from a small trace
  - AbstractionEngine builds prototype categories
  - GapDetector finds missing relations across peers
  - HypothesisEngine emits HYPOTHESIZED_BY edges with confidence < 0.5
  - AnalogyDetector finds structurally isomorphic relations
  - The canonical example: father → father = grandfather
  - CognitionEngine orchestration via consolidate()
  - Provenance: Ed25519 key gen + COSE Sign1 + W3C VC + SCITT
  - Structural multi-hop query follows the father → father chain
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from datetime import datetime, timezone

from cortexm.api.memory import Memory
from cortexm.config import Config
from cortexm.trace.fact import Fact
from cortexm.util import new_id, iso


def _now_iso() -> str:
    return iso(datetime.now(timezone.utc))


def _add_fact(store, subject, relation, value, user_id="alice",
              confidence=0.85, is_derived=False, provenance=None):
    """Insert a fact directly into the trace (bypasses extractor)."""
    fact = Fact(
        id=new_id(),
        subject=subject, relation=relation, value=value,
        valid_from=_now_iso(),
        confidence=confidence, user_id=user_id,
        is_derived=is_derived, memory_type="long_term",
        provenance=provenance or {},
    )
    store.insert_fact(fact)
    store._maybe_commit()
    return fact.id


@pytest.fixture
def mem():
    cfg = Config(db_path=":memory:", cognition_enabled=True,
                 provenance_enabled=True, fade_enabled=False,
                 tmt_enabled=False)
    return Memory(cfg)


# ---------- Cognition Engine -------------------------------------------

def test_pattern_scanner_surfaces_relation_freq(mem):
    """Scanner should find the father relation in a kinship trace."""
    _add_fact(mem.store, "Alice", "father", "Bob")
    _add_fact(mem.store, "Bob", "father", "Charles")
    from cortexm.cognition.scanner import PatternScanner
    scanner = PatternScanner(mem.store)
    res = scanner.run()
    assert res.n_facts_scanned >= 2
    rels = [p.payload.get("relation") for p in res.patterns
            if p.kind == "relation_freq"]
    assert "father" in rels


def test_gap_detector_finds_missing_relation(mem):
    """Alice has father=Bob, Bob has father=Charles, Charles has father=David.
    The chain pattern father→father should be detected; each (start, mid,
    end) where start=Alice, mid=Bob, end=Charles is a structural gap
    for the composite father*father relation."""
    _add_fact(mem.store, "Alice", "father", "Bob")
    _add_fact(mem.store, "Bob", "father", "Charles")
    _add_fact(mem.store, "Charles", "father", "David")
    from cortexm.cognition.scanner import PatternScanner
    from cortexm.cognition.gaps import GapDetector
    scanner = PatternScanner(mem.store)
    scan = scanner.run()
    gaps = GapDetector(mem.store).run(scan)
    structural_gaps = [g for g in gaps.gaps if g.basis == "structural"]
    assert len(structural_gaps) >= 1
    assert any("father" in g.missing_relation for g in structural_gaps)


def test_hypothesis_engine_emits_hypothesized_by_edge(mem):
    """HypothesisEngine should emit a HYPOTHESIZED_BY edge from the
    new hypothesis fact to the supporting fact(s)."""
    _add_fact(mem.store, "Alice", "father", "Bob")
    _add_fact(mem.store, "Bob", "father", "Charles")
    _add_fact(mem.store, "Charles", "father", "David")
    from cortexm.cognition import run_cognition_pass
    report = run_cognition_pass(mem.store, palace=mem.palace)
    assert report.hypotheses.get("facts_added", 0) >= 1
    # all hypotheses should have confidence < 0.5 (per design)
    for h in _all_hypothesis_facts(mem.store):
        assert h["confidence"] < 0.5
        assert h["is_derived"] == 1
    edges = mem.store.conn.execute(
        "SELECT COUNT(*) FROM edges WHERE kind='HYPOTHESIZED_BY'").fetchone()
    assert edges[0] >= 1


def test_cognition_via_consolidate(mem):
    """Memory.consolidate() should run the cognition pass when
    cognition_enabled=True in Config."""
    _add_fact(mem.store, "Alice", "father", "Bob")
    _add_fact(mem.store, "Bob", "father", "Charles")
    out = mem.consolidate()
    dreaming = out.get("dreaming", {})
    cog = dreaming.get("cognition_stats")
    assert cog is not None
    assert "scan" in cog
    assert cog["scan"]["n_facts_scanned"] >= 2


def test_cognition_dry_run_writes_nothing(mem):
    """Dry-run cognition pass should not write any facts to the trace."""
    _add_fact(mem.store, "Alice", "father", "Bob")
    before = mem.store.conn.execute(
        "SELECT COUNT(*) FROM facts").fetchone()[0]
    from cortexm.cognition import run_cognition_pass
    run_cognition_pass(mem.store, palace=mem.palace, dry_run=True)
    after = mem.store.conn.execute(
        "SELECT COUNT(*) FROM facts").fetchone()[0]
    assert before == after


def test_analogy_detector_finds_isomorphic_relations(mem):
    """If two relations have the same fanout pattern across subjects,
    the analogy detector should report an analogy."""
    for who, where in [("alice", "Google"), ("bob", "Stripe"),
                        ("carol", "Anthropic")]:
        _add_fact(mem.store, who, "works_at", where)
        _add_fact(mem.store, who, "studies_at", where)
    from cortexm.cognition.scanner import PatternScanner
    from cortexm.cognition.analogy import AnalogyDetector
    scan = PatternScanner(mem.store).run()
    analogies = AnalogyDetector(mem.store, min_overlap=0.5,
                                  min_support=2).run(scan)
    assert len(analogies.analogies) >= 1
    found = any(
        (a.relation_a == "works_at" and a.relation_b == "studies_at") or
        (a.relation_a == "studies_at" and a.relation_b == "works_at")
        for a in analogies.analogies)
    assert found


# ---------- Provenance stack -------------------------------------------

def test_ed25519_key_generation():
    """Ed25519AgentKey.generate() should produce a valid keypair."""
    from cortexm.provenance.agent import Ed25519AgentKey
    k = Ed25519AgentKey.generate(label="test")
    assert len(k.private_key) == 32
    assert len(k.public_key) == 32
    assert k.did.startswith("did:key:z")
    assert k.label == "test"
    msg = b"hello world"
    sig = k.sign(msg)
    assert k.verify(msg, sig)
    assert not k.verify(b"hello earth", sig)


def test_cose_sign1_round_trip(mem):
    """COSE Sign1 envelope should sign + verify a commit."""
    from cortexm.provenance import (
        Ed25519AgentKey, sign_commit, verify_commit)
    agent = Ed25519AgentKey.generate()
    env = sign_commit(
        commit_id="abc123", chain_hash="def456", n_facts=10,
        agent=agent, extra_payload={"user_id": "alice"})
    assert verify_commit(env, agent=agent,
                            expected_commit_id="abc123",
                            expected_chain_hash="def456")
    assert not verify_commit(env, agent=agent,
                                expected_commit_id="WRONG")


def test_vc_export_and_verify(mem):
    """W3C VC export of a memory range should verify."""
    _add_fact(mem.store, "Alice", "works_at", "Google")
    _add_fact(mem.store, "Alice", "lives_in", "Toronto")
    from cortexm.provenance import (
        export_memory_range_vc, verify_vc, Ed25519AgentKey)
    agent = Ed25519AgentKey.generate()
    vc = export_memory_range_vc(mem.store, user_id="alice", agent=agent)
    assert verify_vc(vc, agent=agent)
    assert vc.credential_subject["n_facts"] >= 1
    assert vc.credential_subject["merkle_root"]
    assert vc.issuer == agent.did


def test_scitt_submit_and_verify(mem):
    """Submit a COSE Sign1 to SCITT, verify the receipt."""
    from cortexm.provenance import (
        Ed25519AgentKey, sign_commit, submit_to_scitt,
        verify_receipt, reset_scitt_log)
    reset_scitt_log()
    agent = Ed25519AgentKey.generate()
    env = sign_commit(
        commit_id="abc123", chain_hash="def456", n_facts=10, agent=agent)
    statement = submit_to_scitt(env)
    assert statement.receipt.leaf_hash
    assert statement.receipt.tree_size >= 1
    assert verify_receipt(statement)


# ---------- Structural multi-hop query --------------------------------

def test_structural_query_grandfather(mem):
    """structural_query(alice, [father, father]) should return Charles
    as the grandfather."""
    _add_fact(mem.store, "Alice", "father", "Bob")
    _add_fact(mem.store, "Bob", "father", "Charles")
    from cortexm.trace.structural import structural_query
    res = structural_query(
        mem.store, mem.palace,
        start_entity="Alice",
        relation_chain=["father", "father"],
        user_id="alice")
    assert res.success
    assert res.final_value == "Charles"
    assert len(res.hops) == 2
    assert all(h.via == "symbolic" for h in res.hops)


def test_structural_query_handles_missing_hop(mem):
    """If a hop has no match, structural_query should abstain gracefully."""
    _add_fact(mem.store, "Alice", "father", "Bob")
    from cortexm.trace.structural import structural_query
    res = structural_query(
        mem.store, mem.palace,
        start_entity="Alice",
        relation_chain=["father", "father"],
        user_id="alice")
    assert not res.success
    assert "no fact matching" in res.failure_reason


# ---------- MCP integration -------------------------------------------

def test_mcp_cognition_run_tool(mem):
    """The MCP contextm_cognition_run tool should run the cognition pass."""
    from cortexm.mcp.server import MCPServer
    server = MCPServer(mem)
    _add_fact(mem.store, "Alice", "father", "Bob")
    _add_fact(mem.store, "Bob", "father", "Charles")
    _add_fact(mem.store, "Charles", "father", "David")
    out = server._cognition_run(user_id="alice")
    assert "scan" in out
    assert out["scan"]["n_facts_scanned"] >= 2


def test_mcp_structural_query_tool(mem):
    """The MCP contextm_structural_query tool should answer grandfather."""
    from cortexm.mcp.server import MCPServer
    server = MCPServer(mem)
    _add_fact(mem.store, "Alice", "father", "Bob")
    _add_fact(mem.store, "Bob", "father", "Charles")
    out = server._structural_query(
        start_entity="Alice",
        relation_chain=["father", "father"],
        user_id="alice")
    assert out["success"]
    assert out["final_value"] == "Charles"


def test_mcp_provenance_export_tool_disabled_by_default():
    """When Config.provenance_enabled=False (default), the MCP tool
    should return a disabled stub, not crash."""
    cfg = Config(db_path=":memory:")
    mem = Memory(cfg)
    from cortexm.mcp.server import MCPServer
    server = MCPServer(mem)
    out = server._provenance_export(user_id="alice")
    assert out.get("disabled") is True


def test_mcp_provenance_export_tool_enabled(mem):
    """When provenance_enabled=True, the MCP tool should produce a
    full VC + COSE + SCITT envelope."""
    from cortexm.mcp.server import MCPServer
    server = MCPServer(mem)
    _add_fact(mem.store, "Alice", "works_at", "Google")
    out = server._provenance_export(user_id="alice")
    assert not out.get("disabled", False)
    assert out["vc_verify"] is True
    assert out["cose_verify"] is True
    assert out["scitt"]["verify"] is True
    assert out["agent_did"].startswith("did:key:z")


def test_mcp_tools_list_has_new_tools():
    """The MCP TOOLS list should include the new tools."""
    from cortexm.mcp.server import TOOLS
    names = [t["name"] for t in TOOLS]
    assert "contextm_provenance_export" in names
    assert "contextm_structural_query" in names
    assert "contextm_cognition_run" in names
    assert "contextm_zk_sql_proof" in names


# ---------- helpers ---------------------------------------------------

def _all_hypothesis_facts(store):
    """Return all derived facts from the cognition engine."""
    rows = store.conn.execute(
        "SELECT id, subject, relation, value, confidence, is_derived, "
        "provenance FROM facts WHERE is_derived=1 AND "
        "provenance LIKE '%\"kind\": \"hypothesis\"%'").fetchall()
    return [{"id": r[0], "subject": r[1], "relation": r[2],
             "value": r[3], "confidence": r[4], "is_derived": r[5]}
            for r in rows]
