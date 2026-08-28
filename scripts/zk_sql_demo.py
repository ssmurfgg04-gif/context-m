"""End-to-end demo for the ZK-SQL proof system (task 7-zksql).

Builds a 10-fact trace with 4 `works_at` facts (Alice @ Google, Bob @ Stripe,
Carol @ Anthropic, Dave @ OpenAI) plus 6 unrelated facts. Then:

  1. Proves COUNT(works_at) == 4 (ZK — verifier sees only the count).
  2. Shows the serialized proof structure (no fact data leaked).
  3. Verifies the proof with the cheap O(1) verifier.
  4. Demonstrates the MCP-server path (`contextm_zk_sql_proof` tool).
"""
from __future__ import annotations
import datetime as dt
import json
from datetime import timezone

from context_m.config import Config
from context_m.api.memory import Memory
from context_m.security.zk_sql import ZkSqlProver
from context_m.trace.fact import make_fact
from context_m.trace.store import TraceStore
from context_m.security.hashes import HashProvider
from context_m.mcp.server import MCPServer


T0 = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)


def build_10_fact_trace():
    store = TraceStore(":memory:", HashProvider())
    store.create_commit(message="demo seed")
    data = [
        # 4 works_at facts
        ("Alice", "works_at", "Google", "alice"),
        ("Bob",   "works_at", "Stripe", "bob"),
        ("Carol", "works_at", "Anthropic", "carol"),
        ("Dave",  "works_at", "OpenAI", "dave"),
        # 6 unrelated facts
        ("Alice", "lives_in", "Toronto", "alice"),
        ("Bob",   "lives_in", "Berlin", "bob"),
        ("Carol", "has_skill", "Python", "carol"),
        ("Dave",  "has_skill", "Rust", "dave"),
        ("Alice", "prefers", "vim", "alice"),
        ("Bob",   "prefers", "emacs", "bob"),
    ]
    facts = []
    for subject, relation, value, user_id in data:
        f = make_fact(subject, relation, value, now=T0, user_id=user_id,
                       source_hash=store.hasher.hash_text(
                           f"{subject}|{relation}|{value}"))
        store.insert_fact(f, commit_id=store.head())
        facts.append(f)
    return store, facts


def main():
    print()
    print("=" * 72)
    print(" ZK-SQL Proof — End-to-End Demo")
    print(" PoneglyphDB-style PLONKish proof over a 10-fact trace")
    print("=" * 72)

    store, facts = build_10_fact_trace()
    print(f"\n[trace] {len(facts)} facts committed, 4 of which have relation 'works_at'")
    print("        (Alice@Google, Bob@Stripe, Carol@Anthropic, Dave@OpenAI)")
    print("        6 unrelated facts (lives_in / has_skill / prefers).")

    prover = ZkSqlProver(store)

    # ---------- COUNT(works_at) -> 4 ----------
    print("\n[prover]  Generating COUNT(works_at) proof …")
    proof = prover.count_proof("works_at")
    print(f"          query          : {proof.query}")
    print(f"          claimed_result : {proof.claimed_result}")
    print(f"          n_facts_committed: {proof.n_facts_committed}")
    print(f"          circuit_gates  : {proof.circuit_gates}")
    print(f"          merkle_root    : {proof.merkle_root[:24]}…")

    # ---------- verify ----------
    print("\n[verifier] Running O(1) verifier (FS + polynomial-identity + HMAC) …")
    ok = prover.verify(proof)
    print(f"            verify => {ok}")

    # ---------- proof report ----------
    print("\n[report]")
    print(prover.proof_report(proof))

    # ---------- no-fact-data-leak check ----------
    blob = proof.serialize()
    parsed = json.loads(blob)
    print("\n[no-leak] Serialized proof JSON structure (top-level keys):")
    print(f"            {list(parsed.keys())}")
    print("          Transcript keys:")
    print(f"            {list(parsed['transcript'].keys())}")
    print(f"          Blob size: {len(blob)} bytes")
    print("\n          Forbidden fact values that must NOT appear in blob:")
    forbidden = ("Google", "Stripe", "Anthropic", "OpenAI",
                 "Toronto", "Berlin", "Python", "Rust", "vim", "emacs")
    leaked = [v for v in forbidden if v in blob]
    for v in forbidden:
        present = v in blob
        marker = "LEAKED" if present else "ok    "
        print(f"            [{marker}] {v!r}")
    assert not leaked, f"data leaked: {leaked}"
    print(f"          OK — no fact values leaked ({len(forbidden)} checked)")

    # ---------- tampering demo ----------
    print("\n[tamper]  Flip claimed_result 4 -> 99, re-verify …")
    from context_m.security.zk_sql import ZkSqlProof
    tampered = ZkSqlProof(
        query=proof.query, claimed_result=99.0,
        merkle_root=proof.merkle_root,
        n_facts_committed=proof.n_facts_committed,
        transcript=proof.transcript, circuit_gates=proof.circuit_gates,
        proof_id=proof.proof_id)
    print(f"          verify(tampered) => {prover.verify(tampered)} (expected False)")

    # ---------- MCP server path ----------
    print("\n[mcp]     Calling contextm_zk_sql_proof via MCPServer …")
    cfg = Config.from_env()
    cfg.zk_sql_enabled = True
    mem = Memory(cfg)
    # Re-seed the same 10-fact trace into the Memory API's store
    for subject, relation, value, user_id in [
        ("Alice", "works_at", "Google", "alice"),
        ("Bob",   "works_at", "Stripe", "bob"),
        ("Carol", "works_at", "Anthropic", "carol"),
        ("Dave",  "works_at", "OpenAI", "dave"),
        ("Alice", "lives_in", "Toronto", "alice"),
        ("Bob",   "lives_in", "Berlin", "bob"),
        ("Carol", "has_skill", "Python", "carol"),
        ("Dave",  "has_skill", "Rust", "dave"),
        ("Alice", "prefers", "vim", "alice"),
        ("Bob",   "prefers", "emacs", "bob"),
    ]:
        from context_m.trace.fact import make_fact as _mk
        f = _mk(subject, relation, value, now=T0, user_id=user_id,
                 source_hash=mem.store.hasher.hash_text(
                     f"{subject}|{relation}|{value}"))
        mem.store.insert_fact(f, commit_id=mem.store.head())

    server = MCPServer(mem)
    # 1) COUNT call
    resp = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "contextm_zk_sql_proof",
                    "arguments": {"query": "count", "relation": "works_at"}}
    })
    payload = resp["result"]["content"][0]["text"]
    out = json.loads(payload)
    print(f"          MCP count result: {out}")
    assert out["verify"] is True
    assert out["claimed_result"] == 4.0
    assert out["n_facts_committed"] == 10

    # 2) Disabled-when-flag-off check
    cfg2 = Config.from_env()  # default zk_sql_enabled=False
    mem2 = Memory(cfg2)
    server2 = MCPServer(mem2)
    resp2 = server2.handle({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "contextm_zk_sql_proof",
                    "arguments": {"query": "count", "relation": "works_at"}}
    })
    out2 = json.loads(resp2["result"]["content"][0]["text"])
    print(f"          MCP (flag off): {out2}")
    assert out2.get("disabled") is True

    # 3) membership proof via MCP
    resp3 = server.handle({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "contextm_zk_sql_proof",
                    "arguments": {"query": "membership",
                                   "subject": "Alice", "relation": "works_at"}}
    })
    out3 = json.loads(resp3["result"]["content"][0]["text"])
    print(f"          MCP membership: {out3}")
    assert out3["verify"] is True
    assert out3["claimed_result"] == 1.0

    print("\n" + "=" * 72)
    print(" All demo checks PASSED.")
    print(" Summary: COUNT(works_at) == 4 proved & verified over 10-fact trace.")
    print("          Zero fact values leaked into the serialized proof.")
    print("          MCP tool `contextm_zk_sql_proof` works (enabled + disabled).")
    print("=" * 72)


if __name__ == "__main__":
    main()
