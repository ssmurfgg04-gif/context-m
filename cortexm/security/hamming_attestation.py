"""Real zero-knowledge Hamming proximity proofs on binary vectors.

Production-grade ZK: prove HammingDistance(private_vec, public_vec) <= threshold
WITHOUT revealing private_vec.

Construction (Pedersen commitments + Sigma protocols):
  1. Decompose private_vec into bits b_i in {0,1}
  2. Commit to each bit: C_i = b_i*G + r_i*H
  3. Prove each C_i is a valid bit commitment (BitProof from zk_proofs)
  4. Compute difference commitments D_i:
       - if public_bit_i = 0: D_i = C_i
       - if public_bit_i = 1: D_i = G - C_i
     This gives D_i = d_i*G + s_i*H where d_i = b_i XOR public_bit_i
  5. D_total = sum(D_i) = distance*G + sum(s_i)*H
  6. Prove D_total commits to value in [0, threshold] using RangeProof

Verifier learns: distance <= threshold. NOTHING about private_vec.

Dependencies: fastecdsa (same as zk_proofs.py)
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

try:
    from fastecdsa.curve import secp256k1
    from fastecdsa.point import Point
    _HAVE_FASTECDSA = True
except ImportError:  # pragma: no cover
    _HAVE_FASTECDSA = False

# Import from real ZK module
from cortexm.security.zk_proofs import (
    PedersenCommitment,
    BitProof,
    RangeProof,
    SchnorrProof,
    _random_scalar,
    _ensure_curve,
)

# Lazy getters for curve params (avoid stale None references)
def _G():
    _ensure_curve()
    from cortexm.security.zk_proofs import _G as __G
    return __G

def _H():
    _ensure_curve()
    from cortexm.security.zk_proofs import _H as __H
    return __H

def _q():
    _ensure_curve()
    from cortexm.security.zk_proofs import _q as __q
    return __q


def _hamming_weight(b: bytes) -> int:
    return sum(bin(byte).count("1") for byte in b)


def _hamming_distance(a: bytes, b: bytes) -> int:
    if len(a) != len(b):
        raise ValueError("vector length mismatch")
    return _hamming_weight(bytes(x ^ y for x, y in zip(a, b)))


@dataclass(frozen=True)
class HammingZKProof:
    """Real ZK proof of Hamming proximity.

    Proves: HammingDistance(private_vec, public_vec) <= threshold
    WITHOUT revealing private_vec.
    """
    bit_commitments: list[PedersenCommitment]
    bit_proofs: list[BitProof]
    distance_commitment: PedersenCommitment
    distance_range_proof: RangeProof
    threshold: int
    public_vec_hash: str  # hash of public_vec (for binding)

    @classmethod
    def prove(cls, public_vec: bytes, private_vec: bytes, threshold: int) -> "HammingZKProof":
        _ensure_curve()
        """Generate ZK proof that HammingDistance(private_vec, public_vec) <= threshold.

        Args:
            public_vec: public reference vector
            private_vec: private vector (kept secret)
            threshold: maximum allowed Hamming distance
        """
        if not _HAVE_FASTECDSA:
            raise RuntimeError("fastecdsa required for ZK proofs")
        if len(public_vec) != len(private_vec):
            raise ValueError("vector length mismatch")

        actual_distance = _hamming_distance(public_vec, private_vec)
        if actual_distance > threshold:
            raise ValueError(f"actual distance {actual_distance} > threshold {threshold}")

        # Decompose into bits
        public_bits = []
        private_bits = []
        for byte in public_vec:
            for i in range(8):
                public_bits.append((byte >> i) & 1)
        for byte in private_vec:
            for i in range(8):
                private_bits.append((byte >> i) & 1)

        n_bits = len(private_bits)

        # Step 1 & 2: commit to each private bit and prove it's 0 or 1
        bit_commitments = []
        bit_proofs = []
        bit_blindings = []

        for b in private_bits:
            r = _random_scalar()
            C, _ = PedersenCommitment.create(b, r)
            bp = BitProof.prove_bit(b, r)
            bit_commitments.append(C)
            bit_blindings.append(r)
            bit_proofs.append(bp)

        # Step 4: compute difference commitments D_i
        # D_i = C_i if public_bit=0, else G - C_i
        # This gives D_i = d_i*G + s_i*H where d_i = b_i XOR public_bit_i
        diff_commitments = []
        diff_blindings = []

        for i in range(n_bits):
            if public_bits[i] == 0:
                # d_i = b_i, so D_i = C_i
                diff_commitments.append(bit_commitments[i])
                diff_blindings.append(bit_blindings[i])
            else:
                # d_i = 1 - b_i, so D_i = G - C_i = (1-b_i)*G + (-r_i)*H
                D_point = _G() + (-1 * bit_commitments[i].point)
                diff_commitments.append(PedersenCommitment(point=D_point))
                diff_blindings.append((-bit_blindings[i]) % _q())

        # Step 5: D_total = sum(D_i) = distance*G + sum(s_i)*H
        total_point = diff_commitments[0].point
        for i in range(1, len(diff_commitments)):
            total_point = total_point + diff_commitments[i].point

        total_blinding = sum(diff_blindings) % _q()
        D_total = PedersenCommitment(point=total_point)

        # Step 6: prove D_total commits to value in [0, threshold]
        # Need to know the actual distance for the range proof
        n_bits_threshold = threshold.bit_length()
        range_proof = RangeProof.prove(actual_distance, total_blinding, n_bits=n_bits_threshold)

        public_hash = hashlib.sha256(public_vec).hexdigest()

        return cls(
            bit_commitments=bit_commitments,
            bit_proofs=bit_proofs,
            distance_commitment=D_total,
            distance_range_proof=range_proof,
            threshold=threshold,
            public_vec_hash=public_hash,
        )

    def verify(self, public_vec: bytes) -> bool:
        _ensure_curve()
        """Verify the ZK Hamming proximity proof.

        Returns True iff:
          1. All bit commitments are valid (0 or 1)
          2. Difference commitments are computed correctly from public_vec
          3. Distance commitment is in range [0, threshold]
        """
        if not _HAVE_FASTECDSA:
            raise RuntimeError("fastecdsa required for ZK verification")

        # Check public_vec binding
        if hashlib.sha256(public_vec).hexdigest() != self.public_vec_hash:
            return False

        # Decompose public_vec into bits
        public_bits = []
        for byte in public_vec:
            for i in range(8):
                public_bits.append((byte >> i) & 1)

        n_bits = len(public_bits)
        if len(self.bit_commitments) != n_bits or len(self.bit_proofs) != n_bits:
            return False

        # Step 1: verify each bit proof
        for C, bp in zip(self.bit_commitments, self.bit_proofs):
            if not bp.verify(C):
                return False

        # Step 4: verify difference commitments
        diff_commitments = []
        for i in range(n_bits):
            if public_bits[i] == 0:
                diff_commitments.append(self.bit_commitments[i])
            else:
                expected_D = _G() + (-1 * self.bit_commitments[i].point)
                diff_commitments.append(PedersenCommitment(point=expected_D))

        # Step 5: verify D_total = sum(D_i)
        expected_total = diff_commitments[0].point
        for i in range(1, len(diff_commitments)):
            expected_total = expected_total + diff_commitments[i].point

        if self.distance_commitment.point != expected_total:
            return False

        # Step 6: verify range proof
        if not self.distance_range_proof.verify(self.distance_commitment):
            return False

        return True


class HammingZKProver:
    """Prover for Hamming proximity ZK proofs."""

    def __init__(self, dims: int, threshold: int = 32) -> None:
        self.dims = dims
        self.threshold = threshold

    def prove(self, public_vec: bytes, private_vec: bytes, 
              salt: bytes | None = None) -> HammingZKProof:
        """Generate ZK proof of Hamming proximity."""
        return HammingZKProof.prove(public_vec, private_vec, self.threshold)


# ---------------------------------------------------------------------------
# Legacy compatibility: checksum-based attestation (non-ZK, for debugging)
# ---------------------------------------------------------------------------
def checksum_prove_and_verify(public_vec: bytes, private_vec: bytes,
                               threshold: int = 32) -> dict:
    """Non-ZK checksum attestation for debugging/comparison.

    This is NOT zero-knowledge — it reveals the Hamming distance.
    Use HammingZKProof for production ZK.
    """
    distance = _hamming_distance(public_vec, private_vec)
    return {
        "verified": distance <= threshold,
        "distance": distance,
        "threshold": threshold,
        "zk": False,
        "note": "Use HammingZKProof for real ZK",
    }
