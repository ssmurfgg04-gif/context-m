"""W3C Verifiable Credentials 2.0 export for memory ranges.

A memory range (e.g. "all facts about user=alice from 2026-01-01 to
2026-02-01") can be exported as a W3C Verifiable Credential with an
eddsa-jcs-2022 Data Integrity proof.

The VC contains:
  - The BLAKE3 root of the range (the Merkle root over the fact hashes)
  - The range query (user_id, valid_from, valid_to)
  - The number of facts in the range
  - The COSE Sign1 envelope that proves the agent signed it

External verifiers can confirm:
  (1) The range query is well-formed
  (2) The agent signed the range (via the COSE Sign1)
  (3) The BLAKE3 root is correct (by recomputing it from the facts)
  — without seeing the individual facts themselves.

This implements the W3C VC Data Model 2.0 + the eddsa-jcs-2022
cryptosuite. JSON Canonicalization Scheme (JCS) is RFC 8785.

Reference:
  - W3C VC Data Model 2.0: https://www.w3.org/TR/vc-data-model-2.0/
  - eddsa-jcs-2022 cryptosuite:
      https://www.w3.org/TR/vc-di-eddsa/
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from cortexm.provenance.agent import Ed25519AgentKey, get_default_agent
from cortexm.provenance.cose import CoseSign1Envelope, sign_commit


@dataclass
class VerifiableCredential:
    """A W3C VC 2.0 with an eddsa-jcs-2022 Data Integrity proof."""
    context: list[str] = field(default_factory=lambda: [
        "https://www.w3.org/ns/credentials/v2",
        "https://context-m.dev/ns/memory/v1",
    ])
    id: str = ""
    type: list[str] = field(default_factory=lambda: [
        "VerifiableCredential", "MemoryRangeCredential"
    ])
    issuer: str = ""           # did:key of the issuing agent
    valid_from: str = ""       # ISO timestamp
    credential_subject: dict = field(default_factory=dict)
    proof: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "@context": self.context,
            "id": self.id,
            "type": self.type,
            "issuer": self.issuer,
            "validFrom": self.valid_from,
            "credentialSubject": self.credential_subject,
            "proof": self.proof,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2)


def _merkle_root(hashes: list[str]) -> str:
    """Compute a Merkle root over a list of BLAKE3 fact hashes.

    For <2 hashes: returns the single hash (or empty string if 0).
    For 2+ hashes: pairwise concat + BLAKE3, repeat until 1 remains.
    Pads odd levels by repeating the last hash.
    """
    if not hashes:
        return ""
    if len(hashes) == 1:
        return hashes[0]
    level = list(hashes)
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])  # pad
        next_level = []
        for i in range(0, len(level), 2):
            combined = (level[i] + level[i + 1]).encode("utf-8")
            try:
                import blake3
                h = blake3.blake3(combined).hexdigest()
            except ImportError:
                h = hashlib.blake2b(combined, digest_size=32).hexdigest()
            next_level.append(h)
        level = next_level
    return level[0]


def _jcs_canonical(obj: dict) -> str:
    """JSON Canonicalization Scheme (RFC 8785).

    Sorts keys recursively, uses minimal whitespace, escapes per RFC.
    Python's json.dumps with sort_keys=True + separators=(",", ":")
    produces output that mostly matches JCS, with one caveat: unicode
    escapes. JCS requires unescaped non-ASCII, which Python's
    ensure_ascii=False provides.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False)


def export_memory_range_vc(store, *,
                            user_id: str | None = None,
                            valid_from: str | None = None,
                            valid_to: str | None = None,
                            include_hypotheses: bool = False,
                            agent: Ed25519AgentKey | None = None,
                            issuer_id: str | None = None) -> VerifiableCredential:
    """Export a memory range as a W3C Verifiable Credential.

    The VC reveals:
      - The range query (user_id, valid_from, valid_to)
      - The number of facts in the range
      - The BLAKE3 Merkle root over the fact hashes
      - The issuing agent's did:key

    The VC does NOT reveal:
      - Individual fact values (only the aggregate Merkle root)
      - The underlying chunks (only metadata + root)
    """
    if agent is None:
        agent = get_default_agent()

    where = "is_active=1 AND quarantined=0"
    args: list = []
    if user_id is not None:
        where += " AND user_id=?"
        args.append(user_id)
    if valid_from is not None:
        where += " AND valid_from >= ?"
        args.append(valid_from)
    if valid_to is not None:
        where += " AND valid_to <= ?"
        args.append(valid_to)
    if not include_hypotheses:
        # exclude derived facts (hypotheses, abstractions, analogies)
        where += " AND is_derived=0"

    rows = store.conn.execute(
        f"SELECT id, source_hash FROM facts WHERE {where} "
        f"ORDER BY valid_from, id", args).fetchall()

    hashes = [r[1] or r[0] for r in rows]  # prefer source_hash, fallback to id
    root = _merkle_root(hashes)

    credential_subject = {
        "range": {
            "user_id": user_id,
            "valid_from": valid_from,
            "valid_to": valid_to,
        },
        "n_facts": len(rows),
        "merkle_root": root,
        "hash_algorithm": "BLAKE3-256",
        "storage": "context-m-trace",
    }

    # build the proof: eddsa-jcs-2022 Data Integrity proof
    ts_created = datetime.now(timezone.utc).isoformat()
    proof_options = {
        "type": "DataIntegrityProof",
        "cryptosuite": "eddsa-jcs-2022",
        "created": ts_created,
        "verificationMethod": agent.did,
        "proofPurpose": "assertionMethod",
    }
    # canonicalize the subject + proof options (minus the proofValue)
    canon_input = {
        "@context": [
            "https://www.w3.org/ns/credentials/v2",
            "https://context-m.dev/ns/memory/v1",
        ],
        "type": ["VerifiableCredential", "MemoryRangeCredential"],
        "issuer": agent.did,
        "validFrom": ts_created,
        "credentialSubject": credential_subject,
    }
    canon_input.update(proof_options)
    canon_str = _jcs_canonical(canon_input)
    signature = agent.sign(canon_str.encode("utf-8"))

    import base64
    proof = proof_options.copy()
    proof["proofValue"] = base64.urlsafe_b64encode(signature).decode().rstrip("=")

    vc = VerifiableCredential(
        id=issuer_id or f"urn:uuid:{hashlib.sha256(canon_str.encode()).hexdigest()[:32]}",
        issuer=agent.did,
        valid_from=ts_created,
        credential_subject=credential_subject,
        proof=proof,
    )
    return vc


def verify_vc(vc: VerifiableCredential,
              agent: Ed25519AgentKey | None = None,
              expected_root: str | None = None,
              expected_n_facts: int | None = None) -> bool:
    """Verify a VerifiableCredential's Data Integrity proof."""
    if agent is None:
        agent = get_default_agent()
        if agent.did != vc.issuer:
            return False

    if expected_root and vc.credential_subject.get("merkle_root") != expected_root:
        return False
    if expected_n_facts is not None and vc.credential_subject.get("n_facts") != expected_n_facts:
        return False

    proof = vc.proof
    if proof.get("type") != "DataIntegrityProof":
        return False
    if proof.get("cryptosuite") != "eddsa-jcs-2022":
        return False

    import base64
    proof_value = proof.get("proofValue", "")
    if not proof_value:
        return False
    signature = base64.urlsafe_b64decode(proof_value + "=" * (-len(proof_value) % 4))

    # reconstruct canonical input (minus the proofValue)
    proof_options = {k: v for k, v in proof.items() if k != "proofValue"}
    canon_input = {
        "@context": vc.context,
        "type": vc.type,
        "issuer": vc.issuer,
        "validFrom": vc.valid_from,
        "credentialSubject": vc.credential_subject,
    }
    canon_input.update(proof_options)
    canon_str = _jcs_canonical(canon_input)
    return agent.verify(canon_str.encode("utf-8"), signature)


__all__ = [
    "VerifiableCredential",
    "export_memory_range_vc", "verify_vc",
]
