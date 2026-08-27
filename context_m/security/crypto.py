"""Encryption at rest — AES-256-GCM envelope encryption.

Threat model: attacker obtains the database file (disk theft, S3 bucket
leak, backup exposure) but not the master key. Every sensitive column —
chunk text (raw conversation), vault payloads, API key hashes' pepper —
is ciphertext in the file; the key never touches disk unless explicitly
exported to a key file (0600) or supplied via ``CONTEXT_M_MASTER_KEY``.

Design (standard envelope):
  master key (KEK)  — 32 bytes, from env / key file / generated
  data key   (DEK)  — 32 bytes, random per database, wrapped by KEK,
                       stored in the ``kv`` table (``enc:dek``)
  payloads          — AES-256-GCM, 12-byte random nonce, AAD-bound

The cipher is injectable into TraceStore (field encryption) and the PII
vault. μ=0 is preserved: all primitives are symmetric, deterministic
given key material, and make zero network or LLM calls.
"""

from __future__ import annotations

import base64
import os
import secrets

try:  # cryptography >= 3.0 ships AESGCM
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAVE_CRYPTO = True
except Exception:  # pragma: no cover — documented degraded mode
    _HAVE_CRYPTO = False

ENV_MASTER_KEY = "CONTEXT_M_MASTER_KEY"


class CryptoUnavailable(RuntimeError):
    pass


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s.encode())


class AESGCMCipher:
    """One cipher object per data key. Encrypt/decrypt strings and bytes."""

    FORMAT = "enc:v1"          # versioned envelope, forward-compatible
    PREFIX = "«enc:v1:"

    def __init__(self, master_key: bytes, dek: bytes | None = None,
                 store=None) -> None:
        if not _HAVE_CRYPTO:
            raise CryptoUnavailable(
                "pip install cryptography  # required for encryption at rest")
        if len(master_key) != 32:
            raise ValueError("master key must be 32 bytes (256-bit)")
        self._kek = master_key
        self._store = store
        if dek is not None:
            self._dek = dek
        elif store is not None:
            self._dek = self._load_or_create_dek(store)
        else:
            self._dek = secrets.token_bytes(32)

    # -------------------------------------------------------------- DEK mgmt
    def _load_or_create_dek(self, store) -> bytes:
        raw = store.kv_get("enc:dek")
        if raw and raw.startswith(f"{self.FORMAT}:"):
            try:
                blob = _b64d(raw[len(self.FORMAT) + 1:])
                nonce, wrapped = blob[:12], blob[12:]
                return AESGCM(self._kek).decrypt(nonce, wrapped, b"context-m-dek")
            except Exception:
                raise CryptoUnavailable(
                    "cannot unwrap data key — wrong master key for this DB?")
        dek = secrets.token_bytes(32)
        nonce = secrets.token_bytes(12)
        wrapped = AESGCM(self._kek).encrypt(nonce, dek, b"context-m-dek")
        store.kv_set("enc:dek", f"{self.FORMAT}:{_b64e(nonce + wrapped)}")
        return dek

    # -------------------------------------------------------------- primitives
    def encrypt(self, plaintext: bytes, aad: bytes = b"") -> str:
        nonce = secrets.token_bytes(12)
        ct = AESGCM(self._dek).encrypt(nonce, plaintext, aad)
        return f"{self.PREFIX}{_b64e(nonce + ct)}»"

    def decrypt(self, envelope: str, aad: bytes = b"") -> bytes:
        if not envelope.startswith(self.PREFIX) or not envelope.endswith("»"):
            raise ValueError("not an encrypted envelope")
        blob = _b64d(envelope[len(self.PREFIX):-1])
        nonce, ct = blob[:12], blob[12:]
        return AESGCM(self._dek).decrypt(nonce, ct, aad)

    def encrypt_str(self, s: str) -> str:
        return self.encrypt(s.encode())

    def decrypt_str(self, envelope: str) -> str:
        return self.decrypt(envelope).decode()

    def is_envelope(self, s: str | None) -> bool:
        return bool(s) and s.startswith(self.PREFIX) and s.endswith("»")

    # -------------------------------------------------------------- rotation
    def rotate(self, new_master_key: bytes) -> bytes:
        """Re-wrap the DEK under a new master key. Returns the DEK so the
        caller can construct a new cipher bound to the same store."""
        if len(new_master_key) != 32:
            raise ValueError("master key must be 32 bytes")
        if self._store is None:
            raise ValueError("rotation requires a store-bound cipher")
        nonce = secrets.token_bytes(12)
        wrapped = AESGCM(new_master_key).encrypt(nonce, self._dek, b"context-m-dek")
        self._store.kv_set("enc:dek", f"{self.FORMAT}:{_b64e(nonce + wrapped)}")
        return self._dek


# ------------------------------------------------------------------ helpers
def load_master_key(path: str | None = None,
                    env: str = ENV_MASTER_KEY) -> bytes | None:
    """Master key resolution order: explicit path, env var (hex/base64),
    sidecar ``<db>.key``. Returns None when no key is configured."""
    if path and os.path.exists(path):
        data = open(path, "rb").read().strip()
        return _coerce_key(data)
    val = os.environ.get(env)
    if val:
        return _coerce_key(val.encode().strip())
    return None


def _coerce_key(data: bytes) -> bytes:
    if len(data) == 32:
        return data
    try:
        raw = _b64d(data.decode())
        if len(raw) == 32:
            return raw
    except Exception:
        pass
    try:
        raw = bytes.fromhex(data.decode())
        if len(raw) == 32:
            return raw
    except Exception:
        pass
    # derive a stable key from arbitrary passphrase material (HKDF-lite)
    import hashlib
    return hashlib.blake2b(data, digest_size=32).digest()


def generate_master_key(path: str) -> bytes:
    """Generate a new master key and persist it 0600 (operator flow)."""
    key = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(_b64e(key).encode())
    return key
