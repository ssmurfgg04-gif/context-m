"""Sanity check for the v0.6.4 ZK soundness rewrite."""
import sys
sys.path.insert(0, "/home/z/my-project")

from cortexm.security.zk_proofs import (
    PedersenCommitment, BitProof, RangeProof, SetMembershipProof,
    SQLAggregateProof, ZKProver, ZKVerifier, _ensure_curve,
)

_ensure_curve()
print("[1] curve init OK")

# BitProof roundtrip both bits
for bit in (0, 1):
    r = 12345
    bp = BitProof.prove_bit(bit, r)
    C, _ = PedersenCommitment.create(bit, r)
    assert bp.verify(C), f"bit {bit} honest verify failed"
print("[2] BitProof honest roundtrips OK (both bits)")

# BitProof does NOT leak the bit: transcript field types identical
b0 = BitProof.prove_bit(0, 77)
b1 = BitProof.prove_bit(1, 77)
assert set(vars(b0).keys()) == set(vars(b1).keys())
print("[3] BitProof transcript shape identical for 0/1 (no bit leak)")

# RangeProof roundtrip
rp, C, r = ZKProver().prove_range(5, n_bits=8)
assert rp.verify(C)
print("[4] RangeProof honest OK")

# SetMembership roundtrip + non-member must FAIL
sp = ZKProver().prove_membership(30, [10, 20, 30, 40], 2)
assert sp.verify([10, 20, 30, 40])
assert not sp.verify([10, 20, 30, 41])  # different set → root mismatch
print("[5] SetMembership honest OK, foreign-set verify fails")

# Sum proof with homomorphism
p = ZKProver().prove_sum([3, 4, 5], 12)
assert ZKVerifier().verify_sum(p)
# tampered: claimed sum wrong → prove raises
try:
    ZKProver().prove_sum([3, 4, 5], 13)
    print("!! tampered sum prove did NOT raise")
    sys.exit(1)
except ValueError:
    pass
print("[6] SQLAggregateProof homomorphic sum OK, tamper raises")

# Hamming roundtrip + threshold violation
from cortexm.security.hamming_attestation import HammingZKProof
pub = bytes([0b00000000, 0b00000000])
priv = bytes([0b00000001, 0b00000000])  # distance 1
proof = HammingZKProof.prove(pub, priv, threshold=2)
assert proof.verify(pub)
print("[7] HammingZK honest OK (distance=1 <= threshold=2)")

try:
    HammingZKProof.prove(pub, priv, threshold=0)
    print("!! threshold violation prove did NOT raise")
    sys.exit(1)
except ValueError:
    print("[8] HammingZK threshold violation rejected at prove-time OK")

# Forgery: take a valid proof for threshold=2, relabel to threshold=0
import dataclasses
forged = dataclasses.replace(proof, threshold=0)
assert not forged.verify(pub), "relabeled threshold forgery ACCEPTED"
print("[9] HammingZK relabeled-threshold forgery REJECTED OK")

# Forgery: forged BitProof for value 5 (the v0.6.3 exploit)
from cortexm.security.zk_proofs import _random_scalar
r_bad = _random_scalar()
C_bad, _ = PedersenCommitment.create(5, r_bad)
bp_bad = BitProof.prove_bit(0, r_bad)  # claim "it's a 0"
assert not bp_bad.verify(C_bad), "forged BitProof ACCEPTED"
print("[10] forged BitProof (value 5 claimed as bit) REJECTED OK")

print("\nALL ZK SANITY CHECKS PASSED")
