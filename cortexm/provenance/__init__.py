"""Provenance Standards Stack — enterprise-grade provenance wrappers.

Context-M's internal integrity is BLAKE3 source hashing + a hash-chained
audit log. This is cryptographically sound but NOT standards-compliant.
Enterprise security reviews ask: "Does it support W3C VC? C2PA? SCITT?"
— this layer answers "yes" by wrapping the existing BLAKE3 hashes into
standards-compliant envelopes.

Three sub-modules:

  cose.py   — COSE Sign1 envelopes (RFC 9052) using Ed25519. Every
              memory commit gets wrapped in a COSE Sign1 with the
              agent's Ed25519 key, so external verifiers can confirm
              the commit came from a known agent without trusting the
              host's audit log.

  vc.py     — W3C Verifiable Credentials 2.0 export. A memory range
              (e.g. "all facts about user=alice from 2026-01-01 to
              2026-02-01") can be exported as a W3C VC Data Integrity
              proof (eddsa-jcs-2022). The VC contains the BLAKE3 root
              of the range + the COSE Sign1 envelope that proves the
              agent signed it.

  scitt.py  — SCITT-signed statements. Forwards a COSE Sign1 to a
              (mocked) SCITT transparency log, returns a receipt that
              can be verified by a third party.

Design: BLAKE3 source hashing stays as the internal integrity mechanism.
This layer SITS ON TOP for external interoperability. No replacement —
just a wrapper. Existing `verify_integrity()` stays; the standards
layer adds `verify_with_standards()` that calls both.

Pure Python — no halo2, no BBS+, no JWT libs. Ed25519 uses the
Python stdlib `hashlib` for hashing and a tiny ed25519 implementation
fallback if `cryptography` is not available. The COSE/VC/SCITT wire
formats are JSON (CBOR is preferred per RFC 9052, but JSON works for
the prototype).
"""

from cortexm.provenance.cose import (
    CoseSign1Envelope,
    sign_commit,
    verify_commit,
)
from cortexm.provenance.vc import (
    VerifiableCredential,
    export_memory_range_vc,
    verify_vc,
)
from cortexm.provenance.scitt import (
    ScittStatement,
    ScittReceipt,
    submit_to_scitt,
    verify_receipt,
    reset_scitt_log,
    get_scitt_service_did,
)
from cortexm.provenance.agent import (
    Ed25519AgentKey,
    get_default_agent,
    set_default_agent,
)

__all__ = [
    # cose
    "CoseSign1Envelope", "Ed25519AgentKey",
    "sign_commit", "verify_commit",
    # vc
    "VerifiableCredential",
    "export_memory_range_vc", "verify_vc",
    # scitt
    "ScittStatement", "ScittReceipt",
    "submit_to_scitt", "verify_receipt",
    "reset_scitt_log", "get_scitt_service_did",
    # agent
    "Ed25519AgentKey",
    "get_default_agent", "set_default_agent",
]
