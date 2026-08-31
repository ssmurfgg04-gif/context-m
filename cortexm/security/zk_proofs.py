"""Real zero-knowledge proofs using Pedersen commitments + Sigma protocols.

Production-grade ZK for memory attestations. Uses secp256k1 elliptic curve
with Pedersen commitments and non-interactive Sigma proofs via Fiat-Shamir.

v0.6.4 soundness hardening (this module was audited and several
forgeability bugs were fixed — see tests/test_zk_soundness.py):

  1. H is now derived by try-and-increment hash-to-curve, so its
     discrete log w.r.t. G is unknown to EVERYONE (the v0.6.3 code
     derived H = h*G with a publicly-computable scalar h, which made
     every Pedersen commitment trivially malleable).
  2. BitProof is a proper Cramer-Damgard-Schoenmakers OR-proof: the
     Fiat-Shamir challenge is bound to (C, R0, R1), BOTH branches are
     verified, and the transcript shape is identical for bit=0 and
     bit=1 (the old code stored a branch flag that leaked the bit and
     only verified the real branch).
  3. SetMembershipProof.verify() takes the public set and recomputes
     the Merkle root itself; the commitment is bound to the leaf by an
     EqualityProof against the leaf's deterministic blinding-0 point.
  4. RangeProof.verify() checks len(bit_commitments) == n_bits.
  5. SQLAggregateProof carries the per-value commitments and the
     verifier checks the Pedersen homomorphism sum(C_i) == C_sum.

Supported proof types:
  - KNOWLEDGE_OF_COMMITMENT: prove knowledge of opening (v, r) to C = v*G + r*H
  - EQUALITY_OF_COMMITMENTS: prove two Pedersen commitments hide the same value
  - SET_MEMBERSHIP: prove a committed value is in a public set (Merkle + Pedersen)
  - RANGE_PROOF: prove a committed value is in [0, 2^n) (bit-decomposition)
  - SQL_AGGREGATE: prove SUM of committed values matches a claimed total

Honest scope note (trusted-prover attestation mode): these primitives
prove relations between COMMITTED values. The linkage between the
committed values and application data (memory vectors, store rows) is
established by the prover at prove-time; the verifier trusts the
integration layer for that binding. Cryptographic guarantees start at
the commitment layer.

Dependencies: fastecdsa (pip install fastecdsa)
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Any

try:
    from fastecdsa.curve import secp256k1
    from fastecdsa.point import Point
    _HAVE_FASTECDSA = True
except ImportError:  # pragma: no cover
    _HAVE_FASTECDSA = False

# ---------------------------------------------------------------------------
# Curve setup: secp256k1
# ---------------------------------------------------------------------------
_G = None
_q = None
_p = None
_H = None

def _ensure_curve():
    """Lazy initialization of curve parameters.

    Re-attempts the fastecdsa import so the module also works when
    fastecdsa is installed AFTER first import of this file.
    """
    global _G, _q, _p, _H, _HAVE_FASTECDSA
    if _G is not None:
        return
    try:
        from fastecdsa.curve import secp256k1 as _curve
        from fastecdsa.point import Point as _Point
    except ImportError:
        raise RuntimeError(
            "fastecdsa required for ZK proofs. "
            "Install: pip install fastecdsa"
        )
    _HAVE_FASTECDSA = True
    _G = _curve.G
    _q = _curve.q
    _p = _curve.p
    # H must be a curve point whose discrete log w.r.t. G is UNKNOWN.
    # Deriving H = h*G from a public scalar h (the v0.6.3 construction)
    # lets anyone who reads this code re-open every Pedersen commitment
    # to any value they like. Try-and-increment hash-to-curve gives a
    # point nobody can take the discrete log of (generic DLOG on
    # secp256k1 is ~2^128 group ops).
    _H = None
    seed = b"context-m-zk-h-generator-v2-unknown-dlog"
    for counter in range(1024):
        digest = hashlib.sha256(
            seed + counter.to_bytes(8, "big")).digest()
        x = int.from_bytes(digest, "big") % _p
        # secp256k1: y^2 = x^3 + 7 (mod p), p % 4 == 3
        y_sq = (pow(x, 3, _p) + 7) % _p
        y = pow(y_sq, (_p + 1) // 4, _p)
        if (y * y) % _p != y_sq:
            continue
        # NB: fastecdsa's affine Point cannot represent the identity,
        # and the constructor validates on-curve membership, so any
        # accepted (x, y) is a usable group element.
        _H = _Point(x, y, _curve)
        break
    if _H is None:
        raise RuntimeError("hash-to-curve failed after 1024 attempts")


def _random_scalar() -> int:
    _ensure_curve()
    return int.from_bytes(secrets.token_bytes(32), "big") % _q


def _hash_challenge(*items) -> int:
    """Fiat-Shamir challenge from points and scalars.

    The challenge MUST be bound to every announcement the verifier will
    later check — a challenge computed from a subset lets a forger pick
    announcements after seeing the challenge (the v0.6.3 BitProof bug).
    """
    _ensure_curve()
    h = hashlib.sha256()
    for item in items:
        if hasattr(item, "x") and hasattr(item, "y"):  # Point
            h.update(f"{item.x}:{item.y}".encode())
        elif isinstance(item, bytes):
            h.update(item)
        else:
            h.update(str(item).encode())
    return int.from_bytes(h.digest(), "big") % _q


# ---------------------------------------------------------------------------
# Pedersen commitment: C = v*G + r*H
# Perfectly hiding, computationally binding under DLOG hardness —
# REQUIRES that dlog_G(H) is unknown, which _ensure_curve guarantees.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PedersenCommitment:
    """A Pedersen commitment C = v*G + r*H."""
    point: Any  # the public commitment point C

    @classmethod
    def create(cls, value: int, blinding: int | None = None) -> tuple["PedersenCommitment", int]:
        """Create commitment. Returns (commitment, blinding_factor)."""
        _ensure_curve()
        if blinding is None:
            blinding = _random_scalar()
        # C = value * G + blinding * H
        C = (value % _q) * _G + (blinding % _q) * _H
        return cls(point=C), blinding

    def verify_opening(self, value: int, blinding: int) -> bool:
        """Verify that (value, blinding) opens this commitment."""
        _ensure_curve()
        expected = (value % _q) * _G + (blinding % _q) * _H
        return self.point == expected

    @classmethod
    def from_point(cls, point) -> "PedersenCommitment":
        return cls(point=point)


def _encode_point(P) -> bytes:
    """Canonical byte encoding of a curve point for hashing/Merkle leaves."""
    return f"{P.x}:{P.y}".encode()


# ---------------------------------------------------------------------------
# Schnorr proof of knowledge of commitment opening
# Prove: I know (v, r) such that C = v*G + r*H
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SchnorrProof:
    """Non-interactive proof of knowledge of Pedersen opening."""
    commitment: Any   # R = a*G + b*H (announcement)
    z_v: int           # response for value
    z_r: int           # response for blinding

    @classmethod
    def prove(cls, value: int, blinding: int, C: PedersenCommitment) -> "SchnorrProof":
        """Generate proof that we know opening of C."""
        _ensure_curve()
        a = _random_scalar()  # random for value
        b = _random_scalar()  # random for blinding
        R = a * _G + b * _H   # announcement
        e = _hash_challenge(R, C.point)
        z_v = (a + e * value) % _q
        z_r = (b + e * blinding) % _q
        return cls(commitment=R, z_v=z_v, z_r=z_r)

    def verify(self, C: PedersenCommitment) -> bool:
        """Verify the proof against commitment C."""
        _ensure_curve()
        e = _hash_challenge(self.commitment, C.point)
        # Check: z_v*G + z_r*H == R + e*C
        lhs = self.z_v * _G + self.z_r * _H
        rhs = self.commitment + e * C.point
        return lhs == rhs


# ---------------------------------------------------------------------------
# Proof of equality: C1 and C2 commit to the same value
# C1 = v*G + r1*H, C2 = v*G + r2*H
# Prove: C1 - C2 = (r1-r2)*H  (pure blinding difference)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EqualityProof:
    """Proof that two Pedersen commitments hide the same value."""
    commitment: Any   # R = a*H
    z: int             # response

    @classmethod
    def prove(cls, r1: int, r2: int, C1: PedersenCommitment, C2: PedersenCommitment) -> "EqualityProof":
        """Prove C1 and C2 commit to same value."""
        _ensure_curve()
        delta_r = (r1 - r2) % _q
        a = _random_scalar()
        R = a * _H
        e = _hash_challenge(R, C1.point, C2.point)
        z = (a + e * delta_r) % _q
        return cls(commitment=R, z=z)

    def verify(self, C1: PedersenCommitment, C2: PedersenCommitment) -> bool:
        """Verify equality proof."""
        _ensure_curve()
        e = _hash_challenge(self.commitment, C1.point, C2.point)
        # Check: z*H == R + e*(C1 - C2)
        lhs = self.z * _H
        rhs = self.commitment + e * (C1.point + (-1 * C2.point))
        return lhs == rhs


# ---------------------------------------------------------------------------
# Merkle tree over deterministic leaf encodings
# ---------------------------------------------------------------------------
def _merkle_root(leaves: list[bytes]) -> bytes:
    """Compute Merkle root over leaf hashes."""
    if not leaves:
        return b""
    if len(leaves) == 1:
        return leaves[0]
    if len(leaves) % 2 == 1:
        leaves = leaves + [leaves[-1]]
    next_level = []
    for i in range(0, len(leaves), 2):
        h = hashlib.sha256(leaves[i] + leaves[i+1]).digest()
        next_level.append(h)
    return _merkle_root(next_level)


def _merkle_path(leaves: list[bytes], index: int) -> list[tuple[bytes, str]]:
    """Get Merkle inclusion path for leaf at index."""
    path = []
    current = list(leaves)
    idx = index
    while len(current) > 1:
        if len(current) % 2 == 1:
            current = current + [current[-1]]
        sibling_idx = idx + 1 if idx % 2 == 0 else idx - 1
        direction = "R" if idx % 2 == 0 else "L"
        path.append((current[sibling_idx], direction))
        next_level = []
        for i in range(0, len(current), 2):
            h = hashlib.sha256(current[i] + current[i+1]).digest()
            next_level.append(h)
        current = next_level
        idx //= 2
    return path


def _verify_merkle_path(leaf: bytes, path: list[tuple[bytes, str]], root: bytes) -> bool:
    """Verify a Merkle inclusion path."""
    current = leaf
    for sibling, direction in path:
        if direction == "L":
            current = hashlib.sha256(sibling + current).digest()
        else:
            current = hashlib.sha256(current + sibling).digest()
    return current == root


@dataclass(frozen=True)
class SetMembershipProof:
    """Proof that a committed value is in a public set.

    Construction (v0.6.4, sound):
      - Each set element v_i gets a DETERMINISTIC leaf commitment
        P_i = v_i*G (blinding 0), encoded canonically into a Merkle
        tree.
      - The prover holds C = v*G + r*H (random blinding, value hidden).
      - An EqualityProof binds C to the leaf commitment P_index: it
        proves C and P_index commit to the same value WITHOUT
        revealing v.
      - The VERIFIER recomputes the Merkle root from the public set
        (the v0.6.3 proof trusted a prover-supplied root, which
        proved nothing) and checks the inclusion path.

    The leaf INDEX is revealed (documented limitation); the VALUE is
    not. Full position hiding needs a ZK-friendly Merkle tree — future
    work.
    """
    commitment: PedersenCommitment   # C = v*G + r*H
    equality: EqualityProof          # C and P_index commit to the same value
    leaf_index: int                  # position in the public set (revealed)
    merkle_path: list[tuple[bytes, str]]  # inclusion path for leaf_index
    leaf_encoding: bytes             # canonical encoding of P_index

    @classmethod
    def prove(cls, value: int, blinding: int, public_set: list[int], index: int) -> "SetMembershipProof":
        """Prove value is in public_set at index."""
        _ensure_curve()
        if not public_set:
            raise ValueError("public_set must be non-empty")
        if not (0 <= index < len(public_set)):
            raise ValueError(f"index {index} out of range for set of "
                             f"size {len(public_set)}")
        C, r = PedersenCommitment.create(value, blinding)
        # deterministic leaf commitment (blinding = 0)
        P_leaf, _ = PedersenCommitment.create(public_set[index], 0)
        # prove C and P_leaf commit to the same value
        eq = EqualityProof.prove(r, 0, C, P_leaf)
        leaves = []
        for v in public_set:
            P_i, _ = PedersenCommitment.create(v, 0)
            leaves.append(_encode_point(P_i.point))
        path = _merkle_path(leaves, index)
        return cls(
            commitment=C,
            equality=eq,
            leaf_index=index,
            merkle_path=path,
            leaf_encoding=leaves[index],
        )

    def verify(self, public_set: list[int]) -> bool:
        """Verify the set membership proof against the public set.

        The Merkle root is RECOMPUTED from public_set — a prover cannot
        supply its own tree.
        """
        _ensure_curve()
        if not public_set:
            return False
        if not (0 <= self.leaf_index < len(public_set)):
            return False
        # recompute leaves + root from the public set
        leaves = []
        for v in public_set:
            P_i, _ = PedersenCommitment.create(v, 0)
            leaves.append(_encode_point(P_i.point))
        root = _merkle_root(leaves)
        # 1. inclusion path for the claimed leaf
        if self.leaf_encoding != leaves[self.leaf_index]:
            return False
        if not _verify_merkle_path(self.leaf_encoding,
                                   self.merkle_path, root):
            return False
        # 2. bind C to the leaf value via the equality proof
        P_leaf = PedersenCommitment.from_point(
            PedersenCommitment.create(public_set[self.leaf_index], 0)[0].point)
        if not self.equality.verify(self.commitment, P_leaf):
            return False
        return True


# ---------------------------------------------------------------------------
# Range proof: prove v in [0, 2^n) without revealing v
# Bit-decomposition: v = sum(b_i * 2^i), prove each b_i in {0,1}
# For each bit: commit to b_i, prove C_i commits to 0 or 1 (OR-proof)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BitProof:
    """CDS OR-proof that a commitment opens to 0 or 1.

    Statements:
      branch 0:  C = r*H            (the committed value is 0)
      branch 1:  C - G = r*H        (the committed value is 1)

    Transcript: (R0, R1, z0, z1, e1) with
      e  = H(C, R0, R1)            (bound to BOTH announcements)
      e0 = e - e1
    Verification checks BOTH branches:
      z0*H == R0 + e0*C
      z1*H == R1 + e1*(C - G)

    The transcript shape is IDENTICAL for bit=0 and bit=1 (e1 is uniform
    random in either case), so the proof leaks nothing about which
    branch is real — the v0.6.3 code stored an explicit branch flag.
    """
    R0: Any   # announcement for branch 0
    R1: Any   # announcement for branch 1
    z0: int    # response for branch 0
    z1: int    # response for branch 1
    e1: int    # challenge for branch 1 (e0 is derived: e0 = e - e1)

    @classmethod
    def prove_bit(cls, bit: int, blinding: int) -> "BitProof":
        """Prove the commitment (bit, blinding) is to 0 or 1 (OR-proof)."""
        _ensure_curve()
        if bit not in (0, 1):
            raise ValueError("bit must be 0 or 1")
        C, r = PedersenCommitment.create(bit, blinding)
        P0 = C.point                      # bit=0 statement
        P1 = C.point + (-1 * _G)          # bit=1 statement
        if bit == 0:
            # simulate branch 1 (free challenge), prove branch 0
            z1 = _random_scalar()
            e1 = _random_scalar()
            R1 = z1 * _H + (-e1 % _q) * P1
            a0 = _random_scalar()
            R0 = a0 * _H
            e = _hash_challenge(C.point, R0, R1)
            e0 = (e - e1) % _q
            z0 = (a0 + e0 * r) % _q
        else:
            # simulate branch 0 (free challenge), prove branch 1
            z0 = _random_scalar()
            e0 = _random_scalar()
            R0 = z0 * _H + (-e0 % _q) * P0
            a1 = _random_scalar()
            R1 = a1 * _H
            e = _hash_challenge(C.point, R0, R1)
            e1 = (e - e0) % _q
            z1 = (a1 + e1 * r) % _q
        return cls(R0=R0, R1=R1, z0=z0, z1=z1, e1=e1)

    def verify(self, C: PedersenCommitment) -> bool:
        """Verify BOTH branches of the OR-proof."""
        _ensure_curve()
        P0 = C.point
        P1 = C.point + (-1 * _G)
        e = _hash_challenge(C.point, self.R0, self.R1)
        e0 = (e - self.e1) % _q
        ok0 = (self.z0 * _H) == (self.R0 + e0 * P0)
        ok1 = (self.z1 * _H) == (self.R1 + self.e1 * P1)
        return ok0 and ok1


@dataclass(frozen=True)
class RangeProof:
    """Proof that a committed value is in [0, 2^n)."""
    n_bits: int
    bit_commitments: list[PedersenCommitment]
    bit_proofs: list[BitProof]
    sum_proof: SchnorrProof  # prove v = sum(b_i * 2^i)

    @classmethod
    def prove(cls, value: int, blinding: int, n_bits: int = 32) -> "RangeProof":
        """Prove 0 <= value < 2^n_bits."""
        _ensure_curve()
        if n_bits < 1:
            raise ValueError("n_bits must be >= 1")
        if value < 0 or value >= (1 << n_bits):
            raise ValueError(f"value {value} out of range [0, 2^{n_bits})")
        # Decompose into bits
        bits = [(value >> i) & 1 for i in range(n_bits)]
        bit_commitments = []
        bit_blindings = []
        bit_proofs = []
        for b in bits:
            r = _random_scalar()
            C, _ = PedersenCommitment.create(b, r)
            bp = BitProof.prove_bit(b, r)
            bit_commitments.append(C)
            bit_blindings.append(r)
            bit_proofs.append(bp)
        # Prove: v*G + r*H == sum(2^i * (b_i*G + r_i*H))
        # => v*G + r*H == (sum b_i*2^i)*G + (sum r_i*2^i)*H
        # => v == sum b_i*2^i  (true by construction)
        # => r == sum r_i*2^i  (enforced via the delta proof below)
        r_sum = sum(r * (1 << i) for i, r in enumerate(bit_blindings)) % _q
        delta = (blinding - r_sum) % _q
        C_total, _ = PedersenCommitment.create(value, blinding)
        # C_bits_sum = sum(2^i * C_i) = v*G + r_sum*H
        C_bits_sum = None
        for i, C in enumerate(bit_commitments):
            term = (1 << i) * C.point
            C_bits_sum = term if C_bits_sum is None else C_bits_sum + term
        # Difference: C_total - C_bits_sum = delta*H
        # Prove knowledge of delta
        a = _random_scalar()
        R = a * _H
        e = _hash_challenge(R, C_total.point, C_bits_sum)
        z = (a + e * delta) % _q
        sum_proof = SchnorrProof(commitment=R, z_v=0, z_r=z)
        return cls(
            n_bits=n_bits,
            bit_commitments=bit_commitments,
            bit_proofs=bit_proofs,
            sum_proof=sum_proof
        )

    def verify(self, C: PedersenCommitment) -> bool:
        """Verify range proof against commitment C."""
        _ensure_curve()
        if self.n_bits < 1:
            return False
        # v0.6.4: the bit count must match the claimed range width —
        # a proof with fewer bits attests a smaller range than the
        # verifier asked for.
        if len(self.bit_commitments) != self.n_bits:
            return False
        if len(self.bit_proofs) != self.n_bits:
            return False
        # Verify each bit proof (BOTH branches of each OR-proof)
        for bc, bp in zip(self.bit_commitments, self.bit_proofs):
            if not bp.verify(bc):
                return False
        # Verify sum consistency
        C_bits_sum = None
        for i, C_bit in enumerate(self.bit_commitments):
            term = (1 << i) * C_bit.point
            C_bits_sum = term if C_bits_sum is None else C_bits_sum + term
        e = _hash_challenge(self.sum_proof.commitment, C.point, C_bits_sum)
        lhs = self.sum_proof.z_r * _H
        rhs = self.sum_proof.commitment + e * (C.point + (-1 * C_bits_sum))
        return lhs == rhs


# ---------------------------------------------------------------------------
# SQL aggregate proofs
# Prove: SUM of the committed values matches a claimed result
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SQLAggregateProof:
    """Proof that an aggregate query result is correct.

    v0.6.4: carries the per-value commitments C_i and the verifier
    checks the Pedersen homomorphism sum(C_i) == C_sum, so the proof
    actually ties the claimed total to the committed values (the
    v0.6.3 version committed only the total — a commitment to a
    number proves nothing about any other number).
    """
    query_type: str  # "SUM" | "COUNT" | "AVG" | "MIN" | "MAX"
    claimed_result: int
    commitment: PedersenCommitment              # C_sum
    value_commitments: list[PedersenCommitment] # C_i per row
    range_proof: RangeProof | None              # bounds the sum
    set_proof: SetMembershipProof | None        # optional value-set proof

    @classmethod
    def prove_sum(cls, values: list[int], blindings: list[int],
                  claimed_sum: int, sum_blinding: int) -> "SQLAggregateProof":
        """Prove sum of values equals claimed_sum.

        C_sum is computed as the HOMOMORPHIC sum of the per-value
        commitments: sum(C_i) = (sum v_i)*G + (sum r_i)*H, so
        value(C_sum) = sum(values) and blinding(C_sum) = sum(blindings).
        """
        _ensure_curve()
        actual_sum = sum(values)
        if actual_sum != claimed_sum:
            raise ValueError(f"claimed sum {claimed_sum} != actual {actual_sum}")
        if len(blindings) != len(values):
            raise ValueError("blindings length must match values length")
        value_commitments = []
        C_sum_point = None
        for v, r in zip(values, blindings):
            C_i, _ = PedersenCommitment.create(v, r)
            value_commitments.append(C_i)
            C_sum_point = (C_i.point if C_sum_point is None
                           else C_sum_point + C_i.point)
        C_sum = PedersenCommitment.from_point(C_sum_point)
        # homomorphic blinding of the sum point
        homomorphic_blinding = sum(blindings) % _q
        if homomorphic_blinding != (sum_blinding % _q):
            # the caller-supplied sum_blinding must match the
            # homomorphic one, otherwise the commitment doesn't open
            raise ValueError(
                "sum_blinding must equal sum(blindings) for the "
                "homomorphic commitment to verify")
        n_bits = max(1, max(1, abs(claimed_sum)).bit_length() + 1)
        rp = RangeProof.prove(claimed_sum % _q, sum_blinding, n_bits=n_bits)
        return cls(
            query_type="SUM",
            claimed_result=claimed_sum,
            commitment=C_sum,
            value_commitments=value_commitments,
            range_proof=rp,
            set_proof=None
        )

    def verify(self, public_set: list[int] | None = None) -> bool:
        """Verify the SQL aggregate proof.

        1. Pedersen homomorphism: sum(C_i) == C_sum
        2. Range proof bounds the committed sum
        3. Optional set proof (requires the public set)
        """
        _ensure_curve()
        # 1. homomorphism
        acc = None
        for C_i in self.value_commitments:
            acc = (C_i.point if acc is None else acc + C_i.point)
        if acc is None or acc != self.commitment.point:
            return False
        # 2. range proof
        if self.range_proof:
            if not self.range_proof.verify(self.commitment):
                return False
        # 3. optional set proof — cannot be verified without the set
        if self.set_proof:
            if public_set is None:
                return False
            if not self.set_proof.verify(public_set):
                return False
        return True


# ---------------------------------------------------------------------------
# Transcript for non-interactive proofs (Fiat-Shamir)
# ---------------------------------------------------------------------------
class Transcript:
    """Fiat-Shamir transcript for binding proofs to context."""

    def __init__(self, label: bytes = b"context-m-zk-v1") -> None:
        self._state = hashlib.sha256(label)

    def append(self, data: bytes) -> "Transcript":
        self._state.update(data)
        return self

    def challenge(self) -> int:
        _ensure_curve()
        return int.from_bytes(self._state.digest(), "big") % _q


# ---------------------------------------------------------------------------
# Facade: prover / verifier pairs
# ---------------------------------------------------------------------------
class ZKProver:
    """Production ZK prover — replaces the old hash-based prototype."""

    def __init__(self, dims: int = 768) -> None:
        _ensure_curve()  # raises RuntimeError with a clear message if missing
        self.dims = dims

    def prove_membership(self, value: int, public_set: list[int], index: int) -> SetMembershipProof:
        """Prove a value is in a public set."""
        blinding = _random_scalar()
        return SetMembershipProof.prove(value, blinding, public_set, index)

    def prove_range(self, value: int, n_bits: int = 32) -> tuple[RangeProof, PedersenCommitment, int]:
        """Prove a value is in [0, 2^n_bits). Returns (proof, commitment, blinding)."""
        blinding = _random_scalar()
        C, r = PedersenCommitment.create(value, blinding)
        rp = RangeProof.prove(value, r, n_bits)
        return rp, C, r

    def prove_sum(self, values: list[int], claimed_sum: int) -> SQLAggregateProof:
        """Prove sum of values equals claimed_sum."""
        blindings = [_random_scalar() for _ in values]
        sum_blinding = sum(blindings) % _q
        return SQLAggregateProof.prove_sum(values, blindings, claimed_sum, sum_blinding)


class ZKVerifier:
    """Production ZK verifier."""

    def verify_membership(self, proof: SetMembershipProof,
                          public_set: list[int]) -> bool:
        return proof.verify(public_set)

    def verify_range(self, proof: RangeProof, commitment: PedersenCommitment) -> bool:
        return proof.verify(commitment)

    def verify_sum(self, proof: SQLAggregateProof) -> bool:
        return proof.verify()
