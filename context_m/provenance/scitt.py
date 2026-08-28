"""SCITT (Secure Coding Infrastructure Transparency) signed statements.

Forwards a COSE Sign1 envelope to a SCITT transparency log, returns a
receipt that can be verified by a third party.

In a production deployment, SCITT runs as an external service (e.g. an
Azure SCITT instance, or a notary.localhost fake service for dev). For
the prototype, we implement an in-process mock SCITT that:
  - accepts a COSE Sign1 envelope
  - appends it to an append-only log (hash-chained)
  - returns a receipt with the leaf hash + the chain head at the time
    of submission + the inclusion path (sibling hashes + direction)
  - signs the receipt with the SCITT service's key

This is NOT a real transparency log (it doesn't gossip, doesn't publish
to a public append-only store) but it implements the API surface that
a real SCITT integration would expose. The signature is verifiable
by any third party that trusts the SCITT service's public key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from context_m.provenance.agent import Ed25519AgentKey
from context_m.provenance.cose import CoseSign1Envelope


@dataclass
class ScittReceipt:
    """A SCITT transparency log receipt."""
    leaf_hash: str                  # hash of this statement in the log
    tree_size: int                  # log size at time of submission
    chain_head: str                 # root hash of the log at time of submission
    # inclusion path: list of (sibling_hash, direction) where direction
    # is 'L' (sibling is to the left of the current node) or 'R' (right)
    inclusion_path: list[tuple[str, str]] = field(default_factory=list)
    service_did: str = ""
    service_signature: str = ""     # base64url Ed25519 sig from SCITT service
    ts: str = ""


@dataclass
class ScittStatement:
    """A SCITT-signed statement (a COSE Sign1 + a SCITT receipt)."""
    envelope: CoseSign1Envelope
    receipt: ScittReceipt
    service_did: str = ""


def _h(data: bytes) -> str:
    """BLAKE3 or BLAKE2b fallback."""
    try:
        import blake3
        return blake3.blake3(data).hexdigest()
    except ImportError:
        return hashlib.blake2b(data, digest_size=32).hexdigest()


def _merkle_level(leaves: list[str]) -> list[str]:
    """Compute the next level up in the Merkle tree."""
    if len(leaves) == 1:
        return leaves
    if len(leaves) % 2 == 1:
        leaves = leaves + [leaves[-1]]  # pad
    out = []
    for i in range(0, len(leaves), 2):
        out.append(_h((leaves[i] + leaves[i + 1]).encode("utf-8")))
    return out


def _merkle_root(leaves: list[str]) -> str:
    """Compute the Merkle root over a list of leaf hashes."""
    if not leaves:
        return ""
    level = list(leaves)
    while len(level) > 1:
        level = _merkle_level(level)
    return level[0]


def _inclusion_path(leaves: list[str], idx: int) -> list[tuple[str, str]]:
    """Build the Merkle inclusion proof path for leaf at idx.

    Returns a list of (sibling_hash, direction) where direction is
    'L' (sibling is to the left of the current node) or 'R' (right).
    The verifier walks the path from the leaf up to the root.
    """
    if idx < 0 or idx >= len(leaves):
        return []
    path: list[tuple[str, str]] = []
    level = list(leaves)
    cur_idx = idx
    while len(level) > 1:
        if len(level) % 2 == 1:
            level = level + [level[-1]]  # pad
        if cur_idx % 2 == 0:
            # current is the left child — sibling is to the right
            sib_idx = cur_idx + 1
            direction = "R"
        else:
            sib_idx = cur_idx - 1
            direction = "L"
        path.append((level[sib_idx], direction))
        level = _merkle_level(level)
        cur_idx //= 2
    return path


def _verify_inclusion(leaf: str,
                       path: list[tuple[str, str]]) -> str:
    """Walk an inclusion path from a leaf, return the recomputed root."""
    cur = leaf
    for sib, direction in path:
        if direction == "R":
            cur = _h((cur + sib).encode("utf-8"))
        else:
            cur = _h((sib + cur).encode("utf-8"))
    return cur


class _ScittLog:
    """In-process mock SCITT transparency log.

    A production SCITT service persists the log to disk + gossips.
    This mock keeps an in-process append-only list. For tests/dev
    only — restart the process and the log is empty (verifications of
    prior receipts will fail since the service key has changed).
    """

    def __init__(self) -> None:
        self._entries: list[str] = []
        self._service_key: Ed25519AgentKey = Ed25519AgentKey.generate(
            label="scitt-mock-service")
        self._service_did = self._service_key.did
        self._lock = threading.Lock()

    @property
    def service_did(self) -> str:
        return self._service_did

    def submit(self, envelope: CoseSign1Envelope) -> ScittReceipt:
        """Submit a COSE Sign1 envelope to the log."""
        with self._lock:
            envelope_b64 = envelope.signature.encode("utf-8")
            leaf = _h(envelope_b64)
            self._entries.append(leaf)
            tree_size = len(self._entries)
            chain_head = _merkle_root(self._entries)
            ts = datetime.now(timezone.utc).isoformat()
            inclusion = _inclusion_path(self._entries, tree_size - 1)

            receipt_data = json.dumps({
                "leaf_hash": leaf,
                "tree_size": tree_size,
                "chain_head": chain_head,
                "ts": ts,
                "service_did": self._service_did,
            }, sort_keys=True).encode("utf-8")
            service_sig = self._service_key.sign(receipt_data)

            return ScittReceipt(
                leaf_hash=leaf,
                tree_size=tree_size,
                chain_head=chain_head,
                inclusion_path=inclusion,
                service_did=self._service_did,
                service_signature=base64.urlsafe_b64encode(
                    service_sig).decode().rstrip("="),
                ts=ts,
            )

    def verify_receipt(self, receipt: ScittReceipt) -> bool:
        """Verify a SCITT receipt against this log's service key."""
        if not receipt.leaf_hash or not receipt.chain_head:
            return False

        sig_b64 = receipt.service_signature
        if not sig_b64:
            return False
        try:
            sig = base64.urlsafe_b64decode(
                sig_b64 + "=" * (-len(sig_b64) % 4))
        except Exception:
            return False

        receipt_data = json.dumps({
            "leaf_hash": receipt.leaf_hash,
            "tree_size": receipt.tree_size,
            "chain_head": receipt.chain_head,
            "ts": receipt.ts,
            "service_did": receipt.service_did,
        }, sort_keys=True).encode("utf-8")

        # verify the service's signature on the receipt metadata
        if receipt.service_did == self._service_did:
            if not self._service_key.verify(receipt_data, sig):
                return False
        else:
            # production: resolve did:key from a registry
            # prototype: reject unknown service DIDs
            return False

        # recompute the Merkle root from the leaf + inclusion path
        recomputed = _verify_inclusion(receipt.leaf_hash,
                                         receipt.inclusion_path)
        return recomputed == receipt.chain_head


# Process-global mock log
_mock_log = _ScittLog()


def submit_to_scitt(envelope: CoseSign1Envelope) -> ScittStatement:
    """Submit a COSE Sign1 envelope to the SCITT transparency log."""
    receipt = _mock_log.submit(envelope)
    return ScittStatement(
        envelope=envelope,
        receipt=receipt,
        service_did=receipt.service_did,
    )


def verify_receipt(statement: ScittStatement) -> bool:
    """Verify a SCITT statement's receipt.

    Verifies:
      1. The service's Ed25519 signature on the receipt metadata
      2. The Merkle inclusion proof (leaf + path = chain_head)

    Returns True only if both checks pass. Production deployments
    would resolve the service key from a did:key registry rather than
    trusting the in-process mock log.
    """
    return _mock_log.verify_receipt(statement.receipt)


def reset_scitt_log() -> None:
    """Reset the in-process SCITT log (test-only)."""
    global _mock_log
    _mock_log = _ScittLog()


def get_scitt_service_did() -> str:
    """Return the SCITT service's DID (for external verification setup)."""
    return _mock_log.service_did


__all__ = [
    "ScittStatement", "ScittReceipt",
    "submit_to_scitt", "verify_receipt",
    "reset_scitt_log", "get_scitt_service_did",
]
