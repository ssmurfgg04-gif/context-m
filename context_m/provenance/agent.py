"""ProvenanceAgent — Ed25519 identity for memory commits.

Each agent (LLM instance, microservice, scheduled job) that writes to
the Trace has an Ed25519 signing key. The agent's public key is
exposed as a `did:key` identifier so external verifiers can resolve
the key from the DID without an external registry.

The agent key is persisted to a tiny PEM-ish file (path from
Config.provenance_agent_key_path). If no path is set, the agent uses
a process-local ephemeral key (regenerated on restart — fine for
tests, not for production cross-system verification).

Uses the `cryptography` library for Ed25519 (constant-time, audited).
If `cryptography` is unavailable, falls back to an HMAC-SHA256 pseudo-
signature clearly labeled "DEV-MODE" — NOT for production.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass


@dataclass
class Ed25519AgentKey:
    """An Ed25519 signing keypair for a provenance agent."""
    private_key: bytes      # 32-byte seed (Ed25519 form)
    public_key: bytes       # 32-byte pubkey
    did: str = ""           # did:key identifier — derived from pubkey
    label: str = "context-m-agent"
    dev_mode: bool = False  # True when using HMAC fallback (no cryptography lib)

    @classmethod
    def generate(cls, label: str = "context-m-agent") -> "Ed25519AgentKey":
        """Generate a fresh Ed25519 keypair."""
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey)
            from cryptography.hazmat.primitives.serialization import (
                Encoding, PrivateFormat, NoEncryption, PublicFormat)
            priv = Ed25519PrivateKey.generate()
            priv_bytes = priv.private_bytes(
                encoding=Encoding.Raw,
                format=PrivateFormat.Raw,
                encryption_algorithm=NoEncryption(),
            )
            pub_bytes = priv.public_key().public_bytes(
                encoding=Encoding.Raw,
                format=PublicFormat.Raw,
            )
            did = _did_key_from_pubkey(pub_bytes)
            return cls(
                private_key=priv_bytes,
                public_key=pub_bytes,
                did=did,
                label=label,
            )
        except ImportError:
            # DEV fallback: HMAC-SHA256 — NOT for production.
            seed = os.urandom(32)
            return cls(
                private_key=seed,
                public_key=seed,  # symmetric — fallback only
                did=f"did:dev:{hashlib.sha256(seed).hexdigest()[:32]}",
                label=label,
                dev_mode=True,
            )

    @classmethod
    def from_pem(cls, pem_path: str,
                 label: str = "context-m-agent") -> "Ed25519AgentKey":
        """Load a keypair from a PEM-ish file. Generates one if missing."""
        if not os.path.exists(pem_path):
            key = cls.generate(label=label)
            key.save_pem(pem_path)
            return key
        try:
            with open(pem_path, "rb") as f:
                text = f.read().decode("utf-8")
            lines = [l.strip() for l in text.splitlines()
                     if l and not l.startswith("-")]
            priv = base64.b64decode(lines[0])
            pub = base64.b64decode(lines[1]) if len(lines) > 1 else b""
            if len(priv) != 32:
                raise ValueError("private key must be 32 bytes")
            if not pub or len(pub) != 32:
                # regenerate pub from priv
                try:
                    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                        Ed25519PrivateKey)
                    from cryptography.hazmat.primitives.serialization import (
                        Encoding, PublicFormat)
                    pub = (Ed25519PrivateKey.from_private_bytes(priv)
                           .public_key().public_bytes(
                               encoding=Encoding.Raw,
                               format=PublicFormat.Raw))
                except ImportError:
                    pub = priv
            did = _did_key_from_pubkey(pub) if len(pub) == 32 else f"did:dev:{hashlib.sha256(priv).hexdigest()[:32]}"
            return cls(
                private_key=priv, public_key=pub, did=did, label=label,
                dev_mode=(len(pub) != 32))
        except Exception:
            key = cls.generate(label=label)
            key.save_pem(pem_path)
            return key

    def save_pem(self, pem_path: str) -> None:
        """Persist the keypair to a tiny PEM-ish file."""
        os.makedirs(os.path.dirname(pem_path) or ".", exist_ok=True)
        text = (
            f"-----BEGIN ED25519 PRIVATE KEY-----\n"
            f"{base64.b64encode(self.private_key).decode()}\n"
            f"-----END ED25519 PRIVATE KEY-----\n"
            f"-----BEGIN ED25519 PUBLIC KEY-----\n"
            f"{base64.b64encode(self.public_key).decode()}\n"
            f"-----END ED25519 PUBLIC KEY-----\n"
            f"LABEL: {self.label}\n"
        )
        with open(pem_path, "w") as f:
            f.write(text)
        try:
            os.chmod(pem_path, 0o600)
        except Exception:
            pass

    def sign(self, message: bytes) -> bytes:
        """Sign a message with the agent's private key."""
        if self.dev_mode:
            # DEV fallback — HMAC-SHA256, clearly labeled
            mac = hmac.new(self.private_key, message, hashlib.sha256).digest()
            return b"DEV-MODE:" + mac
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey)
            priv = Ed25519PrivateKey.from_private_bytes(self.private_key)
            return priv.sign(message)
        except Exception:
            # crypto lib missing at sign time — fall back
            mac = hmac.new(self.private_key, message, hashlib.sha256).digest()
            return b"DEV-MODE:" + mac

    def verify(self, message: bytes, signature: bytes) -> bool:
        """Verify a signature with the agent's public key."""
        if signature.startswith(b"DEV-MODE:"):
            # DEV fallback verify
            expected = hmac.new(self.private_key, message,
                                 hashlib.sha256).digest()
            actual = signature[len(b"DEV-MODE:"):]
            return hmac.compare_digest(expected, actual)
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey)
            pub = Ed25519PublicKey.from_public_bytes(self.public_key)
            try:
                pub.verify(signature, message)
                return True
            except Exception:
                return False
        except ImportError:
            return False


def _did_key_from_pubkey(pubkey: bytes) -> str:
    """Encode a 32-byte Ed25519 pubkey as a did:key identifier.

    Format: did:key:z<base58btc-multibase-prefix><pubkey>
    The multicodec prefix for Ed25519 is 0xed01.
    """
    prefixed = b"\xed\x01" + pubkey
    encoded = _base58btc_encode(prefixed)
    return f"did:key:z{encoded}"


def _base58btc_encode(data: bytes) -> str:
    """Encode bytes in base58 with the Bitcoin alphabet."""
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    num = int.from_bytes(data, "big")
    encoded = ""
    while num > 0:
        num, rem = divmod(num, 58)
        encoded = alphabet[rem] + encoded
    n_zeros = 0
    for b in data:
        if b == 0:
            n_zeros += 1
        else:
            break
    return "1" * n_zeros + encoded


# ---------- default agent registry ------------------------------------

_default_agent = None


def get_default_agent() -> Ed25519AgentKey:
    """Get the process-default agent key. Generates one on first call."""
    global _default_agent
    if _default_agent is None:
        _default_agent = Ed25519AgentKey.generate()
    return _default_agent


def set_default_agent(agent: Ed25519AgentKey) -> None:
    """Set the process-default agent key (e.g. loaded from PEM)."""
    global _default_agent
    _default_agent = agent


__all__ = ["Ed25519AgentKey", "get_default_agent", "set_default_agent"]
