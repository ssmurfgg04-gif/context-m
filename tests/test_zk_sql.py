"""Tests for the ZK-SQL proof system (Halo2/PLONKish-inspired).

Covers the API contract described in task 7-zksql:
  - membership_proof (positive / negative)
  - count_proof (with and without user_id filter)
  - sum_proof on numeric-string values
  - tampered-proof rejection
  - proof_report format
  - unique proof_id
  - no fact-data leak in serialized proof

The verifier path is sublinear (O(1) hash + HMAC), the prover is O(N)
in trace size — documented in the source as a known prototype limitation.
"""
from __future__ import annotations

import datetime as dt
import json
from datetime import timezone

import pytest

from cortexm.config import Config
from cortexm.errors import VerificationError
from cortexm.security.hashes import HashProvider
from cortexm.security.zk_sql_prototype import (ZkSqlProver, ZkSqlProof, CircuitGate,
                                         GATE_TYPES)
from cortexm.trace.fact import make_fact
from cortexm.trace.store import TraceStore


# ---------------------------------------------------------------- fixtures
T0 = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)


def _build_trace(facts_data):
    """Build a TraceStore with the given (subject, relation, value, user_id)
    tuples, all active. Returns (store, fact_list)."""
    store = TraceStore(":memory:", HashProvider())
    store.create_commit(message="seed")
    facts = []
    for subject, relation, value, user_id in facts_data:
        f = make_fact(subject, relation, value, now=T0, user_id=user_id,
                       source_hash=store.hasher.hash_text(f"{subject}|{relation}|{value}"))
        store.insert_fact(f, commit_id=store.head())
        facts.append(f)
    return store, facts


@pytest.fixture
def trace_10():
    """A 10-fact trace with 4 `works_at` facts (Alice + Bob + Carol + Dave
    each work at a different company), plus 6 unrelated facts.

    Used for COUNT(works_at) == 4 over a 10-fact trace (the task's
    end-to-end demo target).
    """
    data = [
        # 4 works_at facts — what we want to count
        ("Alice", "works_at", "Google", "alice"),
        ("Bob",   "works_at", "Stripe", "bob"),
        ("Carol", "works_at", "Anthropic", "carol"),
        ("Dave",  "works_at", "OpenAI", "dave"),
        # 6 unrelated facts — must NOT be counted
        ("Alice", "lives_in", "Toronto", "alice"),
        ("Bob",   "lives_in", "Berlin", "bob"),
        ("Carol", "has_skill", "Python", "carol"),
        ("Dave",  "has_skill", "Rust", "dave"),
        ("Alice", "prefers", "vim", "alice"),
        ("Bob",   "prefers", "emacs", "bob"),
    ]
    return _build_trace(data)


@pytest.fixture
def numeric_trace():
    """A trace where the `salary` relation holds numeric strings, for
    SUM / AVG / MIN / MAX proofs."""
    data = [
        ("Alice", "salary", "120000", "alice"),
        ("Bob",   "salary", "95000", "bob"),
        ("Carol", "salary", "150000", "carol"),
        ("Dave",  "salary", "80000", "dave"),
        ("Alice", "lives_in", "Toronto", "alice"),   # non-numeric, must skip
        ("Bob",   "works_at", "Stripe", "bob"),       # different relation
    ]
    return _build_trace(data)


@pytest.fixture
def alice_two_works():
    """Trace where Alice has 2 works_at facts (multivalue)."""
    data = [
        ("Alice", "works_at", "Google", "alice"),
        ("Alice", "works_at", "Stripe", "alice"),
        ("Bob",   "works_at", "Anthropic", "bob"),
        ("Carol", "works_at", "OpenAI", "carol"),
        ("Dave",  "works_at", "Meta", "dave"),
        ("Bob",   "lives_in", "Berlin", "bob"),
    ]
    return _build_trace(data)


# ---------------------------------------------------------------- tests
def test_membership_proof_positive(trace_10):
    """Prove (Alice, works_at) exists; verify returns True."""
    store, _ = trace_10
    prover = ZkSqlProver(store)
    proof = prover.membership_proof("Alice", "works_at")
    assert proof.claimed_result == 1.0
    assert proof.query.startswith("MEMBERSHIP(Alice,works_at")
    assert prover.verify(proof) is True


def test_membership_proof_negative(trace_10):
    """Prove (Alice, hates) exists — there's no such fact, so the prover
    refuses (raises VerificationError). This is the honest path: a prover
    cannot truthfully issue a proof of existence for a fact that isn't
    there.
    """
    store, _ = trace_10
    prover = ZkSqlProver(store)
    with pytest.raises(VerificationError):
        prover.membership_proof("Alice", "hates")


def test_count_proof(trace_10):
    """COUNT(works_at) == 4 over the 10-fact trace."""
    store, _ = trace_10
    prover = ZkSqlProver(store)
    proof = prover.count_proof("works_at")
    assert proof.claimed_result == 4.0
    assert proof.n_facts_committed == 10
    assert prover.verify(proof) is True


def test_count_proof_filter(alice_two_works):
    """COUNT(works_at, user_id='alice') == 2 (only Alice's works_at)."""
    store, _ = alice_two_works
    prover = ZkSqlProver(store)
    proof = prover.count_proof("works_at", user_id="alice")
    assert proof.claimed_result == 2.0
    assert prover.verify(proof) is True
    # Sanity: the user-filter actually narrowed it down
    full = prover.count_proof("works_at")
    # Alice(2) + Bob(1) + Carol(1) + Dave(1) = 5
    assert full.claimed_result == 5.0


def test_sum_proof(numeric_trace):
    """SUM(salary) where values are numeric strings.
    120000 + 95000 + 150000 + 80000 = 445000
    """
    store, _ = numeric_trace
    prover = ZkSqlProver(store)
    proof = prover.sum_proof("salary")
    assert proof.claimed_result == 445000.0
    assert prover.verify(proof) is True
    # AVG
    avg = prover.avg_proof("salary")
    assert avg.claimed_result == 445000.0 / 4
    assert prover.verify(avg) is True
    # MIN
    mn = prover.minmax_proof("salary", "MIN")
    assert mn.claimed_result == 80000.0
    assert prover.verify(mn) is True
    # MAX
    mx = prover.minmax_proof("salary", "MAX")
    assert mx.claimed_result == 150000.0
    assert prover.verify(mx) is True


def test_sum_proof_with_filter(numeric_trace):
    """SUM over a value_filter substring — narrow the matching set."""
    store, _ = numeric_trace
    prover = ZkSqlProver(store)
    # values "120000", "95000", "150000", "80000" all contain "0" — keep all
    assert prover.sum_proof("salary", value_filter="0").claimed_result == 445000.0
    # values "95000" and "150000" contain "5" — sum = 245000
    assert prover.sum_proof("salary", value_filter="5").claimed_result == 245000.0


def test_verify_tampered(trace_10):
    """Altering the proof's claimed_result must fail verification."""
    store, _ = trace_10
    prover = ZkSqlProver(store)
    proof = prover.count_proof("works_at")
    assert prover.verify(proof) is True
    # Tamper: claim COUNT(works_at) == 99 (lie)
    tampered = ZkSqlProof(
        query=proof.query,
        claimed_result=99.0,                         # <-- altered
        merkle_root=proof.merkle_root,
        n_facts_committed=proof.n_facts_committed,
        transcript=proof.transcript,
        circuit_gates=proof.circuit_gates,
        proof_id=proof.proof_id,
    )
    assert prover.verify(tampered) is False
    # Tamper the transcript commitment
    bad_transcript = dict(proof.transcript)
    bad_transcript["commitment"] = "deadbeef" * 8
    tampered2 = ZkSqlProof(
        query=proof.query,
        claimed_result=proof.claimed_result,
        merkle_root=proof.merkle_root,
        n_facts_committed=proof.n_facts_committed,
        transcript=bad_transcript,
        circuit_gates=proof.circuit_gates,
        proof_id=proof.proof_id,
    )
    assert prover.verify(tampered2) is False


def test_proof_report_format(trace_10):
    """proof_report() produces a string with the query and result."""
    store, _ = trace_10
    prover = ZkSqlProver(store)
    proof = prover.count_proof("works_at")
    report = prover.proof_report(proof)
    assert isinstance(report, str)
    assert "COUNT(works_at)" in report
    assert "4.0" in report or "4" in report
    assert "ZK-SQL Proof Report" in report
    assert "PASS" in report  # verification should pass


def test_proof_id_unique(trace_10):
    """Each proof gets a unique proof_id."""
    store, _ = trace_10
    prover = ZkSqlProver(store)
    p1 = prover.count_proof("works_at")
    p2 = prover.count_proof("works_at")
    p3 = prover.count_proof("lives_in")
    ids = {p1.proof_id, p2.proof_id, p3.proof_id}
    assert len(ids) == 3, f"proof_ids not unique: {ids}"
    assert all(len(pid) > 8 for pid in ids)


def test_no_fact_data_leak(trace_10):
    """Serialized proof does NOT contain the actual fact values.

    The proof may mention the query's relation name ('works_at') and
    the claimed aggregate (the count). It must NOT contain:
      - fact_ids
      - chunk_ids / source_hashes
      - the actual values (e.g. 'Google', 'Stripe', 'Anthropic', 'OpenAI')
      - subjects other than what the query asked about (and even the
        subject is just the query parameter, not a fact_id)
    """
    store, facts = trace_10
    prover = ZkSqlProver(store)
    proof = prover.count_proof("works_at")
    blob = proof.serialize()
    # the JSON blob
    parsed = json.loads(blob)
    # Public fields only:
    assert set(parsed.keys()) == {
        "query", "claimed_result", "merkle_root",
        "n_facts_committed", "transcript", "circuit_gates", "proof_id",
    }
    # The transcript exposes only crypto material + aggregates
    assert set(parsed["transcript"].keys()) == {
        "r", "eval_y", "eval_at_1", "commitment",
        "attestation", "n_matching",
    }
    # The actual fact values must not appear anywhere in the blob
    forbidden_values = {f.value for f in facts}  # Google, Stripe, ...
    forbidden_values |= {f.id for f in facts}     # fact_ids
    forbidden_values |= {f.source_hash for f in facts if f.source_hash}
    leaked = [v for v in forbidden_values if v and v in blob]
    assert not leaked, (
        f"ZK-SQL proof leaked fact data: {leaked}\n"
        f"blob keys: {list(parsed.keys())}\n"
        f"transcript keys: {list(parsed['transcript'].keys())}\n"
        f"blob size: {len(blob)} bytes"
    )
    # Sanity: 'works_at' (the relation name) is in the query — that's OK
    assert "works_at" in blob
    # But specific company names must NOT be in the blob
    for company in ("Google", "Stripe", "Anthropic", "OpenAI",
                     "Toronto", "Berlin", "Python", "Rust", "vim", "emacs"):
        assert company not in blob, (
            f"fact value {company!r} leaked into serialized proof")


def test_circuit_gate_api():
    """The CircuitGate dataclass follows the PLONKish gate equation."""
    g = CircuitGate(q_L=1.0, q_R=0.0, q_O=-1.0, q_M=0.0, q_C=0.0)
    # q_L * w_L + q_R * w_R + q_O * w_O + q_M * w_L*w_R + q_C == 0
    # For w_L = 5, w_O = 5: 1*5 + (-1)*5 = 0 ✓
    w_L, w_R, w_O = 5.0, 0.0, 5.0
    res = g.q_L * w_L + g.q_R * w_R + g.q_O * w_O + g.q_M * w_L * w_R + g.q_C
    assert res == 0.0
    assert "CONST" in GATE_TYPES
    assert "SELECTOR" in GATE_TYPES


def test_empty_trace_count():
    """COUNT on an empty trace returns 0 and verifies."""
    store = TraceStore(":memory:", HashProvider())
    store.create_commit(message="empty")
    prover = ZkSqlProver(store)
    proof = prover.count_proof("works_at")
    assert proof.claimed_result == 0.0
    assert proof.n_facts_committed == 0
    assert prover.verify(proof) is True


def test_membership_with_value():
    """membership_proof with value= pins the specific fact."""
    data = [
        ("Alice", "works_at", "Google", "alice"),
        ("Alice", "works_at", "Stripe", "alice"),
    ]
    store, _ = _build_trace(data)
    prover = ZkSqlProver(store)
    # Prove (Alice, works_at, Google) exists — the value 'Google' appears
    # in the query string (so it IS leaked, by design — the verifier must
    # know what claim they are verifying). The other fact (Stripe) is NOT
    # leaked.
    proof = prover.membership_proof("Alice", "works_at", "Google")
    assert proof.claimed_result == 1.0
    assert prover.verify(proof) is True
    # The non-target value 'Stripe' must NOT appear in the serialized proof
    assert "Stripe" not in proof.serialize()
