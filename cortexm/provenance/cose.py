"""COSE Sign1 envelopes (RFC 9052) for memory commits.

Every memory commit gets wrapped in a COSE Sign1 envelope signed with
the agent's Ed25519 key. External verifiers can confirm the commit came
from a known agent without trusting the host's audit log.

The COSE Sign1 structure (per RFC 9052 §4.1):

    [
        protected: bstr .cbor {1: -8, "alg": "EdDSA"},   // header
        unprotected: {},                                  // empty
        payload: bstr,                                     // commit hash + metadata
        signature: bstr                                    // Ed25519 sig
    ]

We serialize as JSON (CBOR is the spec-preferred format, but JSON works
for the prototype and is easier to inspect).

Payload format (JSON object, base64url-encoded as a bstr):
    {
        "commit_id":  "...",       // Trace commit ID
        "chain_hash": "...",       // BLAKE3 chain hash from the commit
        "n_facts":    int,         // number of facts in the commit
        "ts":         "...",      // ISO timestamp
        "agent_did":  "did:key:..."  // agent identifier
    }
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from cortexm.provenance.agent import Ed25519AgentKey, get_default_agent


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


@dataclass
class CoseSign1Envelope:
    """A COSE Sign1 envelope (RFC 9052) wrapping a memory commit."""
    protected: dict           # protected header (e.g. {"alg": "EdDSA"})
    payload: dict              # decoded payload (commit metadata)
    signature: str = ""        # base64url-encoded signature
    agent_did: str = ""        # did:key of the signing agent
    ts: str = ""               # ISO timestamp when signed
    raw_b64: str = ""           # the full envelope, base64url-encoded

    def to_dict(self) -> dict:
        return {
            "protected": self.protected,
            "payload": self.payload,
            "signature": self.signature,
            "agent_did": self.agent_did,
            "ts": self.ts,
        }

    def to_json(self) -> str:
        """JSON serialization of the envelope."""
        return json.dumps(self.to_dict(), sort_keys=True, indent=2)


def sign_commit(commit_id: str, chain_hash: str, n_facts: int,
                agent: Ed25519AgentKey | None = None,
                extra_payload: dict | None = None) -> CoseSign1Envelope:
    """Sign a memory commit with an Ed25519 agent key.

    Args:
        commit_id: Trace commit ID (hex string)
        chain_hash: BLAKE3 chain hash from the commit
        n_facts: number of facts in the commit
        agent: agent key to sign with. If None, uses the default agent
               (auto-generated on first call; persisted if Config.
               provenance_agent_key_path is set).
        extra_payload: additional fields to include in the payload
                       (e.g. user_id, branch, message).

    Returns:
        CoseSign1Envelope with the signature + payload + protected header.
    """
    if agent is None:
        agent = get_default_agent()

    ts = datetime.now(timezone.utc).isoformat()

    payload = {
        "commit_id": commit_id,
        "chain_hash": chain_hash,
        "n_facts": n_facts,
        "ts": ts,
        "agent_did": agent.did,
        "agent_label": agent.label,
    }
    if extra_payload:
        payload.update(extra_payload)

    protected = {
        "alg": "EdDSA",
        "kid": agent.did,
        "typ": "cortexm-commit",
        "alg_id": -8,  # COSE alg ID for EdDSA
    }

    payload_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    protected_bytes = json.dumps(protected, sort_keys=True).encode("utf-8")

    # COSE Sig_structure (RFC 9052 §4.4):
    #   ["Signature1", protected, external_aad (empty), payload]
    sig_structure = json.dumps(
        ["Signature1",
         _b64url(protected_bytes),
         "",  # external_aad (empty)
         _b64url(payload_bytes)],
        sort_keys=True).encode("utf-8")

    signature = agent.sign(sig_structure)

    envelope = CoseSign1Envelope(
        protected=protected,
        payload=payload,
        signature=_b64url(signature),
        agent_did=agent.did,
        ts=ts,
    )
    # full envelope serialization for storage
    envelope.raw_b64 = _b64url(
        json.dumps(envelope.to_dict(), sort_keys=True).encode("utf-8"))
    return envelope


def verify_commit(envelope: CoseSign1Envelope,
                  agent: Ed25519AgentKey | None = None,
                  expected_commit_id: str | None = None,
                  expected_chain_hash: str | None = None) -> bool:
    """Verify a COSE Sign1 envelope.

    Args:
        envelope: the envelope to verify
        agent: agent key to verify with. If None, a key with the
               envelope's agent_did must be registered (via
               set_default_agent or a future DID resolver).
        expected_commit_id: if set, the payload's commit_id must match
        expected_chain_hash: if set, the payload's chain_hash must match

    Returns:
        True if the signature is valid AND (if set) the expected
        commit_id and chain_hash match.
    """
    if agent is None:
        agent = get_default_agent()
        if agent.did != envelope.agent_did:
            # in production: resolve the key from did:key
            # for now: only verify if the registered agent matches
            return False

    payload = envelope.payload
    if expected_commit_id and payload.get("commit_id") != expected_commit_id:
        return False
    if expected_chain_hash and payload.get("chain_hash") != expected_chain_hash:
        return False

    protected_bytes = json.dumps(envelope.protected, sort_keys=True).encode("utf-8")
    payload_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    sig_structure = json.dumps(
        ["Signature1",
         _b64url(protected_bytes),
         "",
         _b64url(payload_bytes)],
        sort_keys=True).encode("utf-8")

    signature = _b64url_decode(envelope.signature)
    return agent.verify(sig_structure, signature)


def envelope_from_dict(d: dict) -> CoseSign1Envelope:
    """Reconstruct an envelope from a dict (e.g. loaded from JSON)."""
    return CoseSign1Envelope(
        protected=d.get("protected", {}),
        payload=d.get("payload", {}),
        signature=d.get("signature", ""),
        agent_did=d.get("agent_did", ""),
        ts=d.get("ts", ""),
    )


__all__ = [
    "CoseSign1Envelope",
    "sign_commit", "verify_commit",
    "envelope_from_dict",
]
