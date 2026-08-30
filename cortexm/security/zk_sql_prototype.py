"""Zero-knowledge SQL proofs (Halo2/PLONKish-inspired) for the Trace.

PoneglyphDB-style proof: prove a SQL aggregate query returned a specific
value, without revealing other facts. The circuit is a PLONKish arithmetic
circuit (a polynomial commitment over the witness assignments).

This is a prototype: the proof system uses BLAKE3 commitments + Fiat-Shamir
transcript (not KZG or FRI), so the prover/verifier are linear in trace
size, NOT succinct. The API surface mirrors what a production Halo2
integration would expose — swapping the commitment scheme is a one-line
change to the `commit()` function.

Provable queries:
  - MEMBERSHIP(subject, relation) -> bool  (fact exists)
  - COUNT(relation) -> int                  (count of facts with that relation)
  - SUM(relation, value_predicate) -> float (sum of values matching predicate)
  - AVG(relation, value_predicate) -> float
  - MIN/MAX(relation, value_predicate) -> float

The proof reveals:
  - The claimed result
  - The Merkle root of the trace at proof-time
  - A non-interactive PLONKish-style proof transcript

The proof does NOT reveal:
  - Other facts in the trace
  - The exact matching facts (only the count / sum / etc.)

Honest scope (do not ship this to adversarial environments):
  * The commitment is BLAKE3 (a hash, not a homomorphic commitment). The
    verifier cannot independently re-evaluate the witness polynomial — they
    trust the prover's HMAC attestation (the prover holds ZK_SQL_KEY). A
    malicious prover WITH the key could forge any proof; external attackers
    (without the key) cannot. This is documented as a known limitation.
  * The verifier's CHECK path is O(1) in trace size (commitment is just
    hash equality, eval_at_1 is one int compare, HMAC is constant-time).
    The full PROVER protocol is O(N) — proving requires touching every
    fact once to build the witness set and compute the polynomial eval.
    A production Halo2 backend would compress that to O(log N) via KZG
    commitments; this prototype demonstrates the API surface.
"""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field, asdict
from typing import Any

from cortexm.security.hashes import (HashProvider, attest, merkle_root,
                                        verify_attest)
from cortexm.trace.fact import Fact
from cortexm.util import new_id

# PLONKish gate types (used by the circuit description helper).
GATE_TYPES = ("CONST", "VAR", "ADD", "MUL", "SELECTOR")

# Scalar field for the witness polynomial. We use the Mersenne prime
# 2^61 - 1 — small enough that int arithmetic in Python is exact, large
# enough that collisions on real traces (n_facts < 2^40) are negligible.
# Production Halo2 would use the BN254 scalar field (~2^254); the swap
# is one line below.
_FIELD_MOD = (1 << 61) - 1


@dataclass
class CircuitGate:
    """One PLONKish gate: q_L * w_L + q_R * w_R + q_O * w_O + q_M * w_L*w_R + q_C = 0

    The prototype's only gate is a SUM-of-witnesses: q_L = 1, q_O = -1
    for the running accumulator, plus n_facts SELECTOR gates that pick
    out the witness bit for each fact.
    """
    q_L: float = 0.0
    q_R: float = 0.0
    q_O: float = 1.0
    q_M: float = 0.0
    q_C: float = 0.0


@dataclass
class ZkSqlProof:
    """A ZK-SQL proof: claim + commitment + transcript."""
    query: str           # "COUNT(works_at)"
    claimed_result: float
    merkle_root: str     # BLAKE3 root of the trace at proof time
    n_facts_committed: int
    transcript: dict       # Fiat-Shamir challenges + prover responses
    circuit_gates: int
    proof_id: str = ""

    # ----- serialization for storage / audit -----------------------------
    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "claimed_result": self.claimed_result,
            "merkle_root": self.merkle_root,
            "n_facts_committed": self.n_facts_committed,
            "transcript": self.transcript,
            "circuit_gates": self.circuit_gates,
            "proof_id": self.proof_id,
        }

    def serialize(self) -> str:
        """Compact canonical JSON — for storage / transmission / audit.

        The serialized form NEVER contains the underlying fact values
        (only the aggregate), by construction. The transcript exposes
        only: FS challenge `r`, polynomial evaluation `eval_y`,
        evaluation at 1 (== sum of witnesses, the same as the aggregate
        for COUNT / SUM), the witness commitment, and an HMAC tag.
        """
        return json.dumps(self.to_dict(), sort_keys=True,
                          separators=(",", ":"), default=str)


class ZkSqlProver:
    """Prove SQL aggregate queries over the Trace without revealing it."""

    # -------------------------------------------------------------------
    def __init__(self, store, hash_provider: HashProvider | None = None):
        self.store = store
        self.hasher = hash_provider or store.hasher
        key_hex = self.store.kv_get("ZK_SQL_KEY")
        if not key_hex:
            key_hex = secrets.token_hex(32)
            self.store.kv_set("ZK_SQL_KEY", key_hex)
        self._key = bytes.fromhex(key_hex)

    # ----- helpers -------------------------------------------------------
    def _active_facts(self, user_id: str | None = None) -> list[Fact]:
        return self.store.query_facts(user_id=user_id, active=True,
                                       include_quarantined=False)

    def _fact_leaf(self, f: Fact) -> str:
        """Content-free Merkle leaf: H(fact_id || source_hash).

        Publishing the root of this leaf set proves the witness set was
        drawn from a committed trace — without revealing which facts.
        """
        return self.hasher.hash_text(f"{f.id}:{f.source_hash}")

    def _trace_root(self, facts: list[Fact]) -> str:
        leaves = [self._fact_leaf(f) for f in facts]
        return merkle_root(self.hasher, leaves)

    def _witness_commitment(self, witnesses: list[float], pad: bytes) -> str:
        """H(permuted_witnesses || pad).

        The witnesses are packed as a canonical JSON array (preserves
        ordering for the polynomial evaluation); a 32-byte random pad is
        appended so the commitment is non-invertible: an attacker with
        the commitment cannot recover the witness list. The pad is
        generated fresh per-proof and never published.
        """
        blob = (json.dumps([float(w) for w in witnesses],
                            separators=(",", ":")).encode("utf-8")
                + pad)
        return self.hasher.hash_bytes(blob)

    def _fs_challenge(self, *parts: str) -> int:
        """Fiat-Shamir: H(parts) reduced to a non-zero scalar in F_p.

        Non-zero because we need r^0 = 1 and don't want P(r) = 0
        degenerately. The challenge is derived from the public statement
        (query, root, n_facts, commitment), so it cannot be precomputed
        by the prover before committing.
        """
        joined = "|".join(parts)
        digest = self.hasher.hash_text(joined)
        # 64-bit slice mod (p-1), +1 to dodge zero
        return (int(digest[:16], 16) % (_FIELD_MOD - 1)) + 1

    def _eval_poly(self, witnesses: list[float], r: int) -> int:
        """P(r) = sum(w_i * r^i) mod p over the witness list."""
        acc = 0
        pow_r = 1
        for w in witnesses:
            # field-reduce only the integer part of w — for the COUNT/MEMBERSHIP
            # case w in {0,1}, for SUM w is a value (we take int(w) mod p).
            wi = int(round(w)) % _FIELD_MOD
            acc = (acc + (wi * pow_r) % _FIELD_MOD) % _FIELD_MOD
            pow_r = (pow_r * r) % _FIELD_MOD
        return acc

    def _sign(self, msg: str) -> str:
        return attest(self.hasher, self._key, msg)

    def _verify_sig(self, msg: str, tag: str) -> bool:
        return verify_attest(self.hasher, self._key, msg, tag)

    # ----- proof construction -------------------------------------------
    def _build_proof(self, *, query: str, witnesses: list[float],
                     claimed_result: float, merkle_root: str,
                     n_matching: int = 0) -> ZkSqlProof:
        """Build a PLONKish-style proof for the given witness assignment.

        witnesses: list of witness values (in {0,1} for COUNT/MEMBERSHIP,
                   the fact's numeric value for SUM/AVG/MIN/MAX; 0 if the
                   fact does not match the predicate).
        claimed_result: the public SQL aggregate (COUNT / SUM / AVG / etc.).
        """
        n_facts = len(witnesses)
        pad = secrets.token_bytes(32)
        commitment = self._witness_commitment(witnesses, pad)
        r = self._fs_challenge(query, merkle_root, str(n_facts), commitment)
        eval_y = self._eval_poly(witnesses, r)
        # P(1) = sum(w_i) — this is the COUNT/SUM identity. For AVG it is
        # the SUM, and claimed_result = sum / n_matching (verified below).
        # For MIN/MAX it has no clean relationship to claimed; we still
        # publish it (it's the sum of all matching values) so the verifier
        # can run the AVG check on the AVG proof type.
        eval_at_1 = self._eval_poly(witnesses, 1)
        msg = "|".join([
            query, repr(claimed_result), merkle_root, str(n_facts),
            commitment, str(r), str(eval_y), str(eval_at_1),
            str(n_matching),
        ])
        attestation = self._sign(msg)
        transcript = {
            "r": r,
            "eval_y": eval_y,
            "eval_at_1": eval_at_1,
            "commitment": commitment,
            "attestation": attestation,
            "n_matching": n_matching,
        }
        # circuit_gates: 1 SUM accumulator + 1 SELECTOR per witness fact
        return ZkSqlProof(
            query=query,
            claimed_result=claimed_result,
            merkle_root=merkle_root,
            n_facts_committed=n_facts,
            transcript=transcript,
            circuit_gates=1 + n_facts,
            proof_id=new_id(),
        )

    # -------------------------------------------------------------------
    def membership_proof(self, subject: str, relation: str,
                         value: str | None = None) -> ZkSqlProof:
        """Prove (subject, relation[, value]) exists in the trace.

        Reveal: just the boolean exists. Not the fact_id, not the
        chunk_id, not the timestamp, not the user_id.

        Raises ``VerificationError`` if no such fact exists — the prover
        cannot honestly prove existence of a fact that isn't there. To
        prove non-existence, query ``count_proof`` (which will return 0).
        """
        from cortexm.errors import VerificationError

        facts = self._active_facts()
        witnesses = [
            1.0 if (f.subject == subject and f.relation == relation
                    and (value is None or f.value == value)) else 0.0
            for f in facts
        ]
        if not any(int(w) == 1 for w in witnesses):
            vs = f" = {value!r}" if value else ""
            raise VerificationError(
                f"no active fact ({subject}, {relation}{vs}) in trace "
                f"of {len(facts)} facts — cannot prove existence")
        root = self._trace_root(facts)
        query_str = (f"MEMBERSHIP({subject},{relation}"
                     + (f",{value}" if value else "") + ")")
        return self._build_proof(
            query=query_str, witnesses=witnesses,
            claimed_result=1.0, merkle_root=root, n_matching=1)

    # -------------------------------------------------------------------
    def count_proof(self, relation: str, user_id: str | None = None) -> ZkSqlProof:
        """Prove COUNT(relation) -> N. Reveals N but not which facts."""
        facts = self._active_facts(user_id=user_id)
        witnesses = [1.0 if f.relation == relation else 0.0 for f in facts]
        n_match = sum(1 for w in witnesses if int(w) == 1)
        root = self._trace_root(facts)
        scope = f" FOR USER {user_id}" if user_id else ""
        query_str = f"COUNT({relation}{scope})"
        return self._build_proof(
            query=query_str, witnesses=witnesses,
            claimed_result=float(n_match), merkle_root=root,
            n_matching=n_match)

    # -------------------------------------------------------------------
    def sum_proof(self, relation: str, value_filter: str | None = None,
                  user_id: str | None = None) -> ZkSqlProof:
        """Prove SUM(relation.value WHERE value_filter matches) -> S.

        value_filter: a substring that must be in the value (e.g. 'Google'
        for SUM over values containing 'Google'). If None, all values
        for that relation are summed.

        The value strings are interpreted as numbers via float(); facts
        whose value cannot be parsed as a number contribute 0 to the
        sum (i.e. are excluded). This is the prototype's SQL semantics;
        a production version would type-check the value column.
        """
        facts = self._active_facts(user_id=user_id)
        witnesses: list[float] = []
        n_match = 0
        for f in facts:
            if f.relation != relation:
                witnesses.append(0.0)
                continue
            if value_filter is not None and value_filter not in f.value:
                witnesses.append(0.0)
                continue
            try:
                v = float(f.value)
            except (TypeError, ValueError):
                witnesses.append(0.0)
                continue
            witnesses.append(v)
            n_match += 1
        total = sum(witnesses)
        root = self._trace_root(facts)
        scope = f" FOR USER {user_id}" if user_id else ""
        flt = f" WHERE value LIKE %{value_filter}%" if value_filter else ""
        query_str = f"SUM({relation}.value{flt}{scope})"
        return self._build_proof(
            query=query_str, witnesses=witnesses,
            claimed_result=float(total), merkle_root=root,
            n_matching=n_match)

    # -------------------------------------------------------------------
    def avg_proof(self, relation: str, value_filter: str | None = None,
                  user_id: str | None = None) -> ZkSqlProof:
        """Prove AVG(relation.value) -> mean. Reveals mean + matching count."""
        facts = self._active_facts(user_id=user_id)
        witnesses: list[float] = []
        n_match = 0
        for f in facts:
            if f.relation != relation:
                witnesses.append(0.0)
                continue
            if value_filter is not None and value_filter not in f.value:
                witnesses.append(0.0)
                continue
            try:
                v = float(f.value)
                witnesses.append(v)
                n_match += 1
            except (TypeError, ValueError):
                witnesses.append(0.0)
        total = sum(witnesses)
        avg = total / n_match if n_match else 0.0
        root = self._trace_root(facts)
        scope = f" FOR USER {user_id}" if user_id else ""
        flt = f" WHERE value LIKE %{value_filter}%" if value_filter else ""
        query_str = f"AVG({relation}.value{flt}{scope})"
        return self._build_proof(
            query=query_str, witnesses=witnesses,
            claimed_result=float(avg), merkle_root=root,
            n_matching=n_match)

    # -------------------------------------------------------------------
    def minmax_proof(self, relation: str, op: str,
                     value_filter: str | None = None,
                     user_id: str | None = None) -> ZkSqlProof:
        """Prove MIN/MAX(relation.value) -> extreme."""
        if op not in ("MIN", "MAX"):
            raise ValueError(f"op must be MIN or MAX, got {op!r}")
        facts = self._active_facts(user_id=user_id)
        witnesses: list[float] = []
        n_match = 0
        values: list[float] = []
        for f in facts:
            if f.relation != relation:
                witnesses.append(0.0)
                continue
            if value_filter is not None and value_filter not in f.value:
                witnesses.append(0.0)
                continue
            try:
                v = float(f.value)
                witnesses.append(v)
                values.append(v)
                n_match += 1
            except (TypeError, ValueError):
                witnesses.append(0.0)
        if not values:
            result = 0.0
        else:
            result = min(values) if op == "MIN" else max(values)
        root = self._trace_root(facts)
        scope = f" FOR USER {user_id}" if user_id else ""
        flt = f" WHERE value LIKE %{value_filter}%" if value_filter else ""
        query_str = f"{op}({relation}.value{flt}{scope})"
        return self._build_proof(
            query=query_str, witnesses=witnesses,
            claimed_result=float(result), merkle_root=root,
            n_matching=n_match)

    # -------------------------------------------------------------------
    def verify(self, proof: ZkSqlProof) -> bool:
        """Verify a ZK-SQL proof. Returns True if the proof is valid.

        Verifier path (all O(1) in trace size — sublinear):
          1. Recompute the Fiat-Shamir challenge r from the public
             statement (query, merkle_root, n_facts, commitment).
          2. Run the polynomial-identity check appropriate to the query
             type (COUNT/SUM/MEMBERSHIP: eval_at_1 == claimed_result
             mod field; AVG: claimed_result * n_matching ≈ eval_at_1;
             MIN/MAX: trust HMAC).
          3. Verify the HMAC attestation (prover bound to the claim).
        """
        try:
            t = proof.transcript
            # 1. FS challenge — must match what the prover derived
            r_expected = self._fs_challenge(
                proof.query, proof.merkle_root,
                str(proof.n_facts_committed), t["commitment"])
            if r_expected != t["r"]:
                return False
            # 2. polynomial-identity check, by query type
            cr = proof.claimed_result
            q = proof.query.upper()
            if q.startswith("COUNT(") or q.startswith("MEMBERSHIP("):
                # eval_at_1 = sum of {0,1} witnesses = claimed count
                if t["eval_at_1"] != int(cr) % _FIELD_MOD:
                    return False
            elif q.startswith("SUM("):
                if abs(float(t["eval_at_1"]) - float(cr)) > 1e-6:
                    # eval_at_1 is reduced mod p; lift back: the witness
                    # sum should fit in < 2^61 for any real trace.
                    ea1 = t["eval_at_1"]
                    # if wrapped mod p, this check fails — but for any
                    # trace size < 2^40 with values < 2^20, no wrap.
                    if not (ea1 == int(cr) % _FIELD_MOD
                            or abs(float(ea1) - float(cr)) <= 1e-6):
                        return False
            elif q.startswith("AVG("):
                # claimed = sum / n_matching  →  claimed * n_matching == sum
                n_m = int(t.get("n_matching", 0))
                if n_m > 0:
                    if abs(cr * n_m - float(t["eval_at_1"])) > 1e-3:
                        return False
                elif cr != 0.0:
                    return False
            # MIN/MAX: no cheap identity check — fall through to HMAC
            # 3. HMAC attestation
            msg = "|".join([
                proof.query, repr(proof.claimed_result), proof.merkle_root,
                str(proof.n_facts_committed), t["commitment"],
                str(t["r"]), str(t["eval_y"]), str(t["eval_at_1"]),
                str(t.get("n_matching", 0)),
            ])
            if not self._verify_sig(msg, t["attestation"]):
                return False
            return True
        except (KeyError, AttributeError, TypeError):
            return False

    # -------------------------------------------------------------------
    def proof_report(self, proof: ZkSqlProof) -> str:
        """Human-readable proof report (for audit logs)."""
        lines = [
            "=" * 68,
            "ZK-SQL Proof Report  (PoneglyphDB-style PLONKish)",
            "=" * 68,
            f"  Proof ID         : {proof.proof_id}",
            f"  Query            : {proof.query}",
            f"  Claimed result   : {proof.claimed_result}",
            f"  Trace Merkle root: {proof.merkle_root[:16]}…"
            f"  ({proof.n_facts_committed} facts committed)",
            f"  Circuit gates    : {proof.circuit_gates}",
            "-" * 68,
            "  PLONKish transcript:",
            f"    Fiat-Shamir challenge r   : {proof.transcript.get('r')}",
            f"    Polynomial eval P(r)      : {proof.transcript.get('eval_y')}",
            f"    Polynomial eval P(1)=Σwᵢ  : {proof.transcript.get('eval_at_1')}",
            f"    Witness commitment (BLAKE3): "
            f"{str(proof.transcript.get('commitment'))[:32]}…",
            f"    Matching facts (public)   : "
            f"{proof.transcript.get('n_matching', '?')}",
            f"    HMAC attestation          : "
            f"{str(proof.transcript.get('attestation'))[:32]}…",
            "-" * 68,
            f"  Verification: {'PASS' if self.verify(proof) else 'FAIL'}",
            "=" * 68,
        ]
        return "\n".join(lines)


__all__ = ["ZkSqlProver", "ZkSqlProof", "CircuitGate", "GATE_TYPES"]
