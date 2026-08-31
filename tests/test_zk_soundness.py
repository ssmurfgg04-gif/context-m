"""v0.6.4 ZK soundness tests — forgery attempts must FAIL.

These tests encode the concrete attacks found in the v0.6.3 audit
(see worklog 2026-08-31): every one of them USED to verify before
the fix. They are the reason the module was rewritten.

μ note: these are pure crypto tests — no memory, no LLM, no RNG
dependence beyond blinding factors (which never affect verify()).
"""
from __future__ import annotations

import dataclasses
import secrets

import pytest

from cortexm.security.zk_proofs import (
    PedersenCommitment, SchnorrProof, BitProof, RangeProof,
    SetMembershipProof, SQLAggregateProof, ZKProver, ZKVerifier,
    _ensure_curve, _hash_challenge,
)
from cortexm.security.hamming_attestation import HammingZKProof

_ensure_curve()


class TestBitProofSoundness:
    def test_forged_bit_for_value_5_fails(self):
        # v0.6.3 exploit: commit to 5, claim "it's a 0-bit commitment".
        # The old verify() only checked the real branch, so this passed.
        r = secrets.randbelow(2**200)
        C, _ = PedersenCommitment.create(5, r)
        bp = BitProof.prove_bit(0, r)
        assert not bp.verify(C)

    def test_forged_bit_for_value_5_other_branch_fails(self):
        r = secrets.randbelow(2**200)
        C, _ = PedersenCommitment.create(5, r)
        bp = BitProof.prove_bit(1, r)
        assert not bp.verify(C)

    def test_honest_bit_roundtrip_both_values(self):
        for bit in (0, 1):
            r = secrets.randbelow(2**200)
            C, _ = PedersenCommitment.create(bit, r)
            bp = BitProof.prove_bit(bit, r)
            assert bp.verify(C)

    def test_transcript_shape_identical_for_both_bits(self):
        # the v0.6.3 code stored an explicit branch flag (C0/C1 None)
        # that leaked the bit. The v0.6.4 transcript must have the
        # same fields, types and sizes regardless of the bit.
        b0 = BitProof.prove_bit(0, 77)
        b1 = BitProof.prove_bit(1, 77)
        for p in (b0, b1):
            assert p.R0 is not None and p.R1 is not None
            assert p.z0 is not None and p.z1 is not None
        # sizes of the responses are unbounded mod q — compare types
        assert type(b0.z0) is type(b1.z0) is int

    def test_replayed_proof_wrong_commitment_fails(self):
        # a BitProof for commitment C1 must not verify against C2
        r = secrets.randbelow(2**200)
        C1, _ = PedersenCommitment.create(0, r)
        C2, _2 = PedersenCommitment.create(1, secrets.randbelow(2**200))
        bp = BitProof.prove_bit(0, r)
        assert not bp.verify(C2)


class TestRangeProofSoundness:
    def test_bit_count_mismatch_fails(self):
        # v0.6.3: verify() never checked len(bit_commitments) == n_bits,
        # so an 8-bit proof could pose as a 32-bit range attestation.
        rp, C, r = ZKProver().prove_range(5, n_bits=8)
        forged = dataclasses.replace(rp, n_bits=32)
        assert not forged.verify(C)

    def test_honest_roundtrip(self):
        rp, C, r = ZKProver().prove_range(200, n_bits=16)
        assert rp.verify(C)

    def test_proof_against_foreign_commitment_fails(self):
        rp, C, r = ZKProver().prove_range(200, n_bits=16)
        C2, _ = PedersenCommitment.create(201)
        assert not rp.verify(C2)

    def test_value_over_2_pow_n_bits_raises(self):
        with pytest.raises(ValueError):
            ZKProver().prove_range(300, n_bits=8)


class TestSetMembershipSoundness:
    def test_foreign_set_fails(self):
        # v0.6.3: the proof carried its own merkle_root — a proof for
        # value 30 in {10,20,30,40} verified against {10,20,30,41}.
        s = [10, 20, 30, 40]
        proof = ZKProver().prove_membership(30, s, 2)
        assert proof.verify(s)
        assert not proof.verify([10, 20, 30, 41])

    def test_commitment_swap_fails(self):
        s = [10, 20, 30, 40]
        proof = ZKProver().prove_membership(30, s, 2)
        C_bad, _ = PedersenCommitment.create(999)
        forged = dataclasses.replace(proof, commitment=C_bad)
        assert not forged.verify(s)

    def test_leaf_index_out_of_range_fails(self):
        s = [10, 20, 30, 40]
        proof = ZKProver().prove_membership(30, s, 2)
        forged = dataclasses.replace(proof, leaf_index=99)
        assert not forged.verify(s)


class TestHammingSoundness:
    PUB = bytes([0b00000000, 0b00000000])
    PRIV = bytes([0b00000001, 0b00000000])  # distance 1

    def test_honest_roundtrip(self):
        proof = HammingZKProof.prove(self.PUB, self.PRIV, threshold=2)
        assert proof.verify(self.PUB)

    def test_prove_above_threshold_raises(self):
        with pytest.raises(ValueError):
            HammingZKProof.prove(self.PUB, self.PRIV, threshold=0)

    def test_relabelled_threshold_fails(self):
        # v0.6.3: verify() ignored self.threshold entirely; a proof for
        # threshold=2 relabelled to threshold=0 still verified, so the
        # "distance <= threshold" claim was unenforced.
        proof = HammingZKProof.prove(self.PUB, self.PRIV, threshold=2)
        forged = dataclasses.replace(proof, threshold=0)
        assert not forged.verify(self.PUB)

    def test_public_vector_swap_fails(self):
        proof = HammingZKProof.prove(self.PUB, self.PRIV, threshold=2)
        other = bytes([0b00000000, 0b00000100])
        assert not proof.verify(other)

    def test_empty_vectors_raise(self):
        with pytest.raises(ValueError):
            HammingZKProof.prove(b"", b"", threshold=4)

    def test_slack_link_tamper_fails(self):
        # tamper the link response → the D_slack equation breaks
        proof = HammingZKProof.prove(self.PUB, self.PRIV, threshold=2)
        forged = dataclasses.replace(
            proof, slack_link_z=(proof.slack_link_z + 1))
        assert not forged.verify(self.PUB)


class TestPedersenBinding:
    def test_commitment_malleability_is_gone(self):
        # v0.6.3 exploit: H = h*G with public h meant (42, 7) also
        # opened as (999, r') for a computable r'. With hash-to-curve
        # H (unknown DLOG) there is no such r' — verify_opening fails.
        C, r = PedersenCommitment.create(42, 7)
        assert C.verify_opening(42, 7)
        assert not C.verify_opening(999, 7)
        # brute force a few alternative blindings — none should open
        for r_prime in (7, 8, 42, 0, 999, 12345):
            assert not C.verify_opening(999, r_prime)
            assert not C.verify_opening(43, r_prime)

    def test_schnorr_roundtrip_and_tamper(self):
        C, r = PedersenCommitment.create(42)
        sp = SchnorrProof.prove(42, r, C)
        assert sp.verify(C)
        tampered = dataclasses.replace(sp, z_v=(sp.z_v + 1))
        assert not tampered.verify(C)


class TestZKFacade:
    def test_prove_range_roundtrip(self):
        prover = ZKProver()
        rp, C, r = prover.prove_range(12345, n_bits=32)
        assert ZKVerifier().verify_range(rp, C)

    def test_prove_membership_roundtrip(self):
        s = [5, 10, 15, 20, 25]
        proof = ZKProver().prove_membership(15, s, 2)
        assert ZKVerifier().verify_membership(proof, s)

    def test_prove_sum_roundtrip(self):
        proof = ZKProver().prove_sum([3, 4, 5], 12)
        assert ZKVerifier().verify_sum(proof)

    def test_prove_sum_wrong_claim_raises(self):
        with pytest.raises(ValueError):
            ZKProver().prove_sum([3, 4, 5], 13)
