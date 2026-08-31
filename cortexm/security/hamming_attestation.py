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

v0.6.4 threshold enforcement (the v0.6.3 code attested
"distance < 2^ceil(log2(threshold))" — e.g. threshold=5 proved
distance < 8 — and verify() never looked at the threshold at all):

  6. The prover publishes a slack commitment
         D_slack = threshold*G + T*H - D_total
       which commits to slack = threshold - distance with blinding
       (T - S), S = sum(s_i).
  7. A range proof proves slack in [0, 2^k). Since the bit proofs
     already bound distance to [0, n_bits] (a sum of 0/1 values),
     slack >= 0 is exactly distance <= threshold.
  8. A Schnorr-style proof over base H binds D_slack to the public
     equation (knowledge of T such that D_slack + D_total -
     threshold*G = T*H), so the prover cannot publish a slack
     commitment disconnected from D_total.

Verifier learns: distance <= threshold for the committed bit
decomposition. The binding between the committed bits and the actual
private vector is trusted-prover (attestation mode) — see the honest
scope note in zk_proofs.py.

Dependencies: fastecdsa (same as zk_proofs.py)
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cortexm.security.zk_proofs import (
    PedersenCommitment,
    BitProof,
    RangeProof,
    _random_scalar,
    _ensure_curve,
    _hash_challenge,
)

# Lazy getters for curve params (avoid stale None references)
def _G():
    _ensure_curve()
    import cortexm.security.zk_proofs as _zk
    return _zk._G

def _H():
    _ensure_curve()
    import cortexm.security.zk_proofs as _zk
    return _zk._H

def _q():
    _ensure_curve()
    import cortexm.security.zk_proofs as _zk
    return _zk._q


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
    distance_commitment: PedersenCommitment          # D_total
    slack_commitment: PedersenCommitment             # D_slack (v0.6.4)
    slack_range_proof: RangeProof                    # slack in [0, 2^k)
    slack_link_R: object                             # announcement for the T-proof
    slack_link_z: int                                # response for the T-proof
    threshold: int
    public_vec_hash: str  # hash of public_vec (for binding)

    @classmethod
    def prove(cls, public_vec: bytes, private_vec: bytes, threshold: int) -> "HammingZKProof":
        """Generate ZK proof that HammingDistance(private_vec, public_vec) <= threshold."""
        _ensure_curve()
        if len(public_vec) != len(private_vec):
            raise ValueError("vector length mismatch")
        if not public_vec:
            raise ValueError("empty vectors are not provable")
        if threshold < 0:
            raise ValueError("threshold must be >= 0")

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

        # Steps 1-2: commit to each private bit and prove it's 0 or 1
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

        # Step 4: difference commitments D_i = C_i if public_bit=0, else G - C_i
        # D_i = d_i*G + s_i*H where d_i = b_i XOR public_bit_i
        diff_commitments = []
        diff_blindings = []

        for i in range(n_bits):
            if public_bits[i] == 0:
                diff_commitments.append(bit_commitments[i])
                diff_blindings.append(bit_blindings[i])
            else:
                D_point = _G() + (-1 * bit_commitments[i].point)
                diff_commitments.append(PedersenCommitment(point=D_point))
                diff_blindings.append((-bit_blindings[i]) % _q())

        # Step 5: D_total = sum(D_i) = distance*G + S*H
        total_point = diff_commitments[0].point
        for i in range(1, len(diff_commitments)):
            total_point = total_point + diff_commitments[i].point
        total_blinding = sum(diff_blindings) % _q()
        D_total = PedersenCommitment(point=total_point)

        # Steps 6-8 (v0.6.4): slack = threshold - distance >= 0.
        # D_slack = threshold*G + T*H - D_total
        T = _random_scalar()
        slack_point = (threshold % _q()) * _G() + T * _H() + (-1 * D_total.point)
        D_slack = PedersenCommitment(point=slack_point)
        slack = threshold - actual_distance  # >= 0, checked above
        slack_blinding = (T - total_blinding) % _q()
        k = max(1, threshold.bit_length())
        slack_range_proof = RangeProof.prove(slack, slack_blinding, n_bits=k)

        # link proof: knowledge of T s.t. X = D_slack + D_total - threshold*G = T*H
        X = D_slack.point + D_total.point + (-(threshold % _q()) * _G())
        a = _random_scalar()
        R_link = a * _H()
        e = _hash_challenge(R_link, X)
        z_link = (a + e * T) % _q()

        public_hash = hashlib.sha256(public_vec).hexdigest()

        return cls(
            bit_commitments=bit_commitments,
            bit_proofs=bit_proofs,
            distance_commitment=D_total,
            slack_commitment=D_slack,
            slack_range_proof=slack_range_proof,
            slack_link_R=R_link,
            slack_link_z=z_link,
            threshold=threshold,
            public_vec_hash=public_hash,
        )

    def verify(self, public_vec: bytes) -> bool:
        """Verify the ZK Hamming proximity proof.

        Returns True iff:
          1. All bit commitments are valid (0 or 1) — BOTH branches of
             each OR-proof are checked.
          2. D_total is the correct difference commitment sum for
             public_vec's bits (so D_total commits to the distance).
          3. The slack commitment is bound by the link proof and its
             range proof, giving distance <= threshold.
        """
        _ensure_curve()
        if self.threshold < 0:
            return False

        # Check public_vec binding
        if hashlib.sha256(public_vec).hexdigest() != self.public_vec_hash:
            return False
        if not public_vec:
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

        # Step 4: recompute difference commitments from public bits
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

        # Steps 6-8 (v0.6.4): the slack machinery — this is what
        # actually enforces distance <= threshold.
        # X = D_slack + D_total - threshold*G must equal T*H for a T
        # the prover knows (checked via the link proof below).
        X = (self.slack_commitment.point + self.distance_commitment.point
             + (-(self.threshold % _q()) * _G()))
        e = _hash_challenge(self.slack_link_R, X)
        lhs = self.slack_link_z * _H()
        rhs = self.slack_link_R + e * X
        if lhs != rhs:
            return False
        # range proof: value(D_slack) in [0, 2^k) — combined with the
        # link equation this yields distance <= threshold.
        if not self.slack_range_proof.verify(self.slack_commitment):
            return False

        return True


class HammingZKProver:
    """Prover for Hamming proximity ZK proofs."""

    def __init__(self, dims: int, threshold: int = 32) -> None:
        _ensure_curve()
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
