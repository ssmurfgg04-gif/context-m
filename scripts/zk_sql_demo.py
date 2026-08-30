"""Demo of real zero-knowledge proofs for memory attestations.

This demo shows the production-grade ZK proof system:
  - Pedersen commitments on secp256k1
  - Schnorr proofs of knowledge
  - Range proofs for SQL aggregates
  - Hamming proximity proofs for binary vectors

Dependencies: fastecdsa
"""
from __future__ import annotations

import secrets

from cortexm.security.zk_proofs import (
    ZKProver, ZKVerifier,
    PedersenCommitment, SchnorrProof,
    RangeProof, SetMembershipProof,
    SQLAggregateProof,
)
from cortexm.security.hamming_attestation import HammingZKProof


def demo_pedersen_commitment():
    print("=== Pedersen Commitment Demo ===")
    value = 42
    blinding = secrets.randbelow(2**256)
    C, r = PedersenCommitment.create(value, blinding)
    print(f"Value: {value}")
    print(f"Commitment point: ({C.point.x}, {C.point.y})")
    assert C.verify_opening(value, r)
    print("Opening verified!\n")


def demo_schnorr_proof():
    print("=== Schnorr Proof of Knowledge Demo ===")
    value = 100
    blinding = secrets.randbelow(2**256)
    C, r = PedersenCommitment.create(value, blinding)
    proof = SchnorrProof.prove(value, blinding, C)
    assert proof.verify(C)
    print(f"Proved knowledge of opening for commitment to {value}")
    print("Proof verified!\n")


def demo_range_proof():
    print("=== Range Proof Demo ===")
    value = 50
    blinding = secrets.randbelow(2**256)
    C, r = PedersenCommitment.create(value, blinding)
    rp = RangeProof.prove(value, r, n_bits=8)
    assert rp.verify(C)
    print(f"Proved {value} is in range [0, 256)")
    print("Range proof verified!\n")


def demo_set_membership():
    print("=== Set Membership Proof Demo ===")
    public_set = [10, 20, 30, 40, 50]
    value = 30
    index = 2
    proof = SetMembershipProof.prove(value, secrets.randbelow(2**256), public_set, index)
    assert proof.verify()
    print(f"Proved {value} is in set {public_set}")
    print("Set membership proof verified!\n")


def demo_sql_aggregate():
    print("=== SQL Aggregate Proof Demo ===")
    values = [10, 20, 30, 40]
    claimed_sum = 100
    blindings = [secrets.randbelow(2**256) for _ in values]
    sum_blinding = sum(blindings) % (2**256)
    proof = SQLAggregateProof.prove_sum(values, blindings, claimed_sum, sum_blinding)
    assert proof.verify()
    print(f"Proved SUM({values}) = {claimed_sum}")
    print("SQL aggregate proof verified!\n")


def demo_hamming_proximity():
    print("=== Hamming Proximity ZK Proof Demo ===")
    public_vec = b"\x00\x01\x02\x03"
    private_vec = b"\x00\x01\x02\x04"  # differs by 1 bit
    threshold = 8
    proof = HammingZKProof.prove(public_vec, private_vec, threshold)
    assert proof.verify(public_vec)
    print(f"Proved HammingDistance(private, public) <= {threshold}")
    print("Hamming proximity ZK proof verified!\n")


if __name__ == "__main__":
    print("Context-M Real ZK Proof Demo\n")
    print("All proofs use Pedersen commitments on secp256k1\n")

    demo_pedersen_commitment()
    demo_schnorr_proof()
    demo_range_proof()
    demo_set_membership()
    demo_sql_aggregate()
    demo_hamming_proximity()

    print("All demos passed! Production-grade ZK is working.")
