"""Cryptographic primitives: BLAKE3 hashing, Merkle trees, attestations.

Spec requirement (Section 1.1 / InjecMEM defense): every fact carries a
BLAKE3 hash of its source text; on retrieval the hash is re-verified, and
Memory-Git commits form a tamper-evident hash chain. BLAKE2b-256 is the
automatic fallback when the optional ``blake3`` wheel is absent — the
provider name is always reported so audits stay honest.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

try:  # optional dependency, preferred per spec
    import blake3 as _blake3

    _HAS_BLAKE3 = True
except Exception:  # pragma: no cover - environment dependent
    _blake3 = None
    _HAS_BLAKE3 = False

_log = logging.getLogger("cortexm.security")
_DOWNGRADE_WARNED = False


def has_blake3() -> bool:
    return _HAS_BLAKE3


def _warn_downgrade() -> None:
    """Loud, once-per-process warning when BLAKE3 is unavailable.

    Silent capability downgrade is a credibility bug: the docs promise
    BLAKE3 and the user must know when they are actually getting
    BLAKE2b. Install with ``pip install context-m[blake3]`` to silence.
    """
    global _DOWNGRADE_WARNED
    if not _DOWNGRADE_WARNED:
        _DOWNGRADE_WARNED = True
        _log.warning(
            "blake3 wheel not installed — downgrading hash provider to "
            "BLAKE2b-256. Integrity guarantees hold (collision resistance "
            "is equivalent at 256-bit) but this differs from the documented "
            "default. Fix: pip install 'context-m[blake3]'. "
            "The active provider is always reported in stats() and audit "
            "output as hash_provider."
        )


class HashProvider:
    """BLAKE3-256 (preferred) or BLAKE2b-256 (fallback). Same interface."""

    def __init__(self, algo: str = "blake3") -> None:
        if algo == "blake3" and not _HAS_BLAKE3:
            _warn_downgrade()
            algo = "blake2b"
        if algo not in ("blake3", "blake2b"):
            raise ValueError(f"unsupported hash provider {algo!r}")
        self.algo = algo

    @property
    def name(self) -> str:
        return "blake3-256" if self.algo == "blake3" else "blake2b-256"

    def hash_bytes(self, data: bytes) -> str:
        if self.algo == "blake3":
            return _blake3.blake3(data).hexdigest()
        return hashlib.blake2b(data, digest_size=32).hexdigest()

    def hash_text(self, text: str) -> str:
        return self.hash_bytes(text.encode("utf-8"))

    def hash_json(self, obj) -> str:
        payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
        return self.hash_text(payload)

    def short(self, hexdigest: str, n: int = 8) -> str:
        return hexdigest[:n]


# --------------------------------------------------------------------------
# Merkle trees (binary, duplicate-last-when-odd) — used by ZK-lite proofs.
# --------------------------------------------------------------------------

def _h_pair(provider: HashProvider, a: str, b: str) -> str:
    if len(a) == 64 and len(b) == 64:
        try:
            return provider.hash_bytes(bytes.fromhex(a) + bytes.fromhex(b))
        except ValueError:
            pass
    return provider.hash_text(a + b)


def merkle_root(provider: HashProvider, leaves: list[str]) -> str:
    if not leaves:
        return provider.hash_text("empty")
    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [_h_pair(provider, level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


def merkle_proof(provider: HashProvider, leaves: list[str], index: int) -> tuple[str, list[dict]]:
    """Return (root, path) where path = [{side, hash}, ...] for leaves[index]."""
    if not leaves or not (0 <= index < len(leaves)):
        raise IndexError("index out of range")
    level = list(leaves)
    idx = index
    path: list[dict] = []
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        sib = idx ^ 1
        path.append({"side": "right" if sib > idx else "left", "hash": level[sib]})
        level = [_h_pair(provider, level[i], level[i + 1]) for i in range(0, len(level), 2)]
        idx //= 2
    return level[0], path


def merkle_verify(provider: HashProvider, leaf: str, path: list[dict], root: str) -> bool:
    cur = leaf
    for step in path:
        if step["side"] == "right":
            cur = _h_pair(provider, cur, step["hash"])
        else:
            cur = _h_pair(provider, step["hash"], cur)
    return hmac.compare_digest(cur, root)


def attest(provider: HashProvider, key: bytes, message: str) -> str:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_attest(provider: HashProvider, key: bytes, message: str, tag: str) -> bool:
    return hmac.compare_digest(attest(provider, key, message), tag)
