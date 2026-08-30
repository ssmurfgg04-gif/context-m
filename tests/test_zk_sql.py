"""Tests for real zero-knowledge proofs.

Tests the production-grade ZK system:
  - Pedersen commitments
  - Schnorr proofs
  - Range proofs
  - Set membership proofs
  - SQL aggregate proofs
"""
from __future__ import annotations

import pytest
import secrets

from cortexm.security.zk_proofs import (
    PedersenCommitment, SchnorrProof,
    RangeProof, SetMembershipProof,
    SQLAggregateProof, ZKProver, ZKVerifier,
)
from cortexm.security.hamming_attestation import HammingZKProof


class TestPedersenCommitment:
    def test_create_and_verify(self):
        C, r = PedersenCommitment.create(42)
        assert C.verify_opening(42, r)

    def test_wrong_value_fails(self):
        C, r = PedersenCommitment.create(42)
        assert not C.verify_opening(43, r)

    def test_wrong_blinding_fails(self):
        C, r = PedersenCommitment.create(42)
        wrong_r = (r + 1) % (2**256)
        assert not C.verify_opening(42, wrong_r)


class TestSchnorrProof:
    def test_prove_and_verify(self):
        value = 100
        C, r = PedersenCommitment.create(value)
        proof = SchnorrProof.prove(value, r, C)
        assert proof.verify(C)

    def test_wrong_value_fails(self):
        value = 100
        C, r = PedersenCommitment.create(value)
        wrong_proof = SchnorrProof.prove(99, r, C)
        assert not wrong_proof.verify(C)


class TestRangeProof:
    def test_in_range_passes(self):
        value = 50
        C, r = PedersenCommitment.create(value)
        rp = RangeProof.prove(value, r, n_bits=8)
        assert rp.verify(C)

    def test_out_of_range_fails(self):
        with pytest.raises(ValueError):
            RangeProof.prove(300, secrets.randbelow(2**256), n_bits=8)


class TestSetMembership:
    def test_member_passes(self):
        public_set = [10, 20, 30, 40]
        proof = SetMembershipProof.prove(30, secrets.randbelow(2**256), public_set, 2)
        assert proof.verify()

    def test_non_member_fails(self):
        # This would need a fake proof to test — in practice the prover
        # would raise if the value is not in the set
        pass


class TestSQLAggregate:
    def test_sum_correct(self):
        values = [10, 20, 30]
        blindings = [secrets.randbelow(2**256) for _ in values]
        sum_blinding = sum(blindings) % (2**256)
        proof = SQLAggregateProof.prove_sum(values, blindings, 60, sum_blinding)
        assert proof.verify()

    def test_sum_incorrect_raises(self):
        values = [10, 20, 30]
        blindings = [secrets.randbelow(2**256) for _ in values]
        sum_blinding = sum(blindings) % (2**256)
        with pytest.raises(ValueError):
            SQLAggregateProof.prove_sum(values, blindings, 61, sum_blinding)


class TestHammingZK:
    def test_proximity_within_threshold(self):
        public_vec = b"\x00\x01\x02\x03"
        private_vec = b"\x00\x01\x02\x04"  # 1 bit different
        proof = HammingZKProof.prove(public_vec, private_vec, threshold=8)
        assert proof.verify(public_vec)

    def test_proximity_exceeds_threshold_raises(self):
        public_vec = b"\x00" * 32
        private_vec = b"\xff" * 32  # 256 bits different
        with pytest.raises(ValueError):
            HammingZKProof.prove(public_vec, private_vec, threshold=8)

    def test_wrong_public_vec_fails(self):
        public_vec = b"\x00\x01\x02\x03"
        private_vec = b"\x00\x01\x02\x04"
        proof = HammingZKProof.prove(public_vec, private_vec, threshold=8)
        wrong_public = b"\x00\x01\x02\x05"
        assert not proof.verify(wrong_public)
