"""v0.6.4 MCP ZK tool regression tests.

The v0.6.3 refactor deleted the old ZK API but left the two MCP tools
calling it — every invocation raised NameError (swallowed by the broad
except into a JSON error). These tests pin the fixed behavior.
"""
from __future__ import annotations

import json

from cortexm import Memory
from cortexm.config import Config


def _server():
    from cortexm.mcp.server import MCPServer
    m = Memory(Config(db_path=":memory:", zk_sql_enabled=True))
    # numeric facts via the store API (the tool test targets the MCP
    # layer, not the extractor's numeric pattern coverage)
    from cortexm.trace.fact import make_fact
    from datetime import datetime, timezone
    commit = m.store.create_commit("mcp-zk-test")
    for v in (30, 40, 50):
        f = make_fact("user:u1", "age", str(v),
                      now=datetime.now(timezone.utc), user_id="u1")
        m.store.insert_fact(f, commit)
        m.palace.add(f.id, m.palace.encode_fact(f))
    m.reader.invalidate_caches()
    return MCPServer(m), m


def test_zk_sql_proof_sum_tool():
    srv, m = _server()
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "contextm_zk_sql_proof",
                      "arguments": {"query": "sum",
                                    "relation": "age",
                                    "user_id": "u1"}}}
    resp = srv.handle(req)
    parsed = json.loads(resp["result"]["content"][0]["text"])
    # v0.6.3: this always returned {'error': "name 'ZkSqlProver' is not
    # defined"} — the tool crashed on a NameError from a deleted module.
    assert "error" not in parsed, parsed
    assert parsed["query"] == "SUM"
    assert parsed["claimed_result"] == 120  # 30 + 40 + 50
    assert parsed["verify"] is True
    m.close()


def test_zk_sql_proof_membership_tool():
    srv, m = _server()
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "contextm_zk_sql_proof",
                      "arguments": {"query": "membership",
                                    "relation": "age",
                                    "value": "40",
                                    "user_id": "u1"}}}
    resp = srv.handle(req)
    parsed = json.loads(resp["result"]["content"][0]["text"])
    assert "error" not in parsed, parsed
    assert parsed["verify"] is True
    m.close()


def test_zk_sql_proof_unsupported_query_is_honest():
    srv, m = _server()
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "contextm_zk_sql_proof",
                      "arguments": {"query": "avg",
                                    "relation": "age",
                                    "user_id": "u1"}}}
    resp = srv.handle(req)
    parsed = json.loads(resp["result"]["content"][0]["text"])
    # unsupported aggregates must produce an explicit error message,
    # not a crash
    assert "error" in parsed
    assert "COUNT/AVG/MIN/MAX" in parsed["error"]
    m.close()


def test_zk_prove_tool_runs():
    srv, m = _server()
    # find a memory in the palace to prove about
    palace = m.palace
    mem_id = next(iter(palace._id2row)) if palace._id2row else None
    if mem_id is None:
        m.close()
        return  # no palace contents — nothing to prove, skip gracefully
    row = palace._id2row[mem_id]
    packed = palace._packed[row]
    public = bytes(packed.tobytes())
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "contextm_zk_prove",
                      "arguments": {"memory_id": mem_id,
                                    "public_commitment": public.hex(),
                                    "threshold": 64}}}
    resp = srv.handle(req)
    parsed = json.loads(resp["result"]["content"][0]["text"])
    # v0.6.3: always {'error': "name 'HammingZKProver' is not defined"}
    assert "error" not in parsed, parsed
    assert parsed["verified"] is True
    assert parsed["threshold"] == 64
    m.close()


def test_zk_sql_proof_disabled_by_default():
    from cortexm.mcp.server import MCPServer
    m = Memory(Config(db_path=":memory:"))  # zk_sql_enabled default off
    srv = MCPServer(m)
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "contextm_zk_sql_proof",
                      "arguments": {"query": "sum",
                                    "relation": "works_at"}}}
    resp = srv.handle(req)
    parsed = json.loads(resp["result"]["content"][0]["text"])
    assert parsed.get("disabled") is True
    m.close()
