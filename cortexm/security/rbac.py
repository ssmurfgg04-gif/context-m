"""Role-based access control and API key management.

Enterprise procurement asks two questions before anything else: "who can
read the memory?" and "can we prove what they did?". This module answers
the first; ``cortexm.enterprise.audit`` answers the second.

Roles (least-privilege ladder):
  admin    — everything, including key management and erasure
  operator — memory read/write, snapshots; no keys, no erasure
  reader   — search/get only
  auditor  — audit log and integrity verification only

API keys look like ``ctxm_<role>_<32 hex>`` and are stored ONLY as
BLAKE3 digests with a per-deployment pepper — a leaked database cannot
be used to authenticate. Verification is constant-time.
"""

from __future__ import annotations

import hmac
import secrets
import time

from cortexm.security.hashes import HashProvider

ROLES = ("admin", "operator", "reader", "auditor")

# action -> minimum role allowed (admin passes everything)
PERMISSIONS: dict[str, set[str]] = {
    "memory.add":         {"admin", "operator"},
    "memory.search":      {"admin", "operator", "reader"},
    "memory.get":         {"admin", "operator", "reader"},
    "memory.get_all":     {"admin", "operator", "reader"},
    "memory.update":      {"admin", "operator"},
    "memory.delete":      {"admin", "operator"},
    "memory.delete_all":  {"admin"},           # destructive
    "memory.history":     {"admin", "operator", "reader"},
    "memory.stats":       {"admin", "operator", "reader"},
    "memory.verify":      {"admin", "operator", "reader", "auditor"},
    "memory.export":      {"admin", "operator", "reader"},  # swappable decoder
    "memory.chaos_ingest": {"admin", "operator"},  # EAM chaos mode
    "audit.read":         {"admin", "auditor"},
    "audit.verify":       {"admin", "auditor"},
    "keys.create":        {"admin"},
    "keys.list":          {"admin"},
    "keys.revoke":        {"admin"},
    "sparql.query":       {"admin", "operator", "reader"},
    "governance.erase":   {"admin"},           # GDPR right-to-erasure
    "governance.retention": {"admin"},
    "governance.snapshot":  {"admin", "operator"},
    "governance.restore":   {"admin"},
    "governance.pitr":      {"admin", "operator", "reader"},
    "governance.consolidate": {"admin"},  # dreaming pass trigger
    "federation.digest":  {"admin", "operator", "reader", "auditor"},
    "federation.sync":    {"admin", "operator"},  # CRDT envelope exchange
}

_KEY_PREFIX = "ctxm"


class RBACError(PermissionError):
    def __init__(self, action: str, role: str) -> None:
        super().__init__(
            f"role {role!r} is not allowed to perform {action!r}")
        self.action = action
        self.role = role


class APIKeyStore:
    """Persistent API key registry (kv-backed, peppered digests)."""

    def __init__(self, store, pepper: bytes | None = None) -> None:
        self.store = store
        self._hasher = HashProvider("blake2b")
        self._pepper = pepper or self._load_pepper()

    def _load_pepper(self) -> bytes:
        raw = self.store.kv_get("rbac:pepper")
        if raw:
            return bytes.fromhex(raw)
        p = secrets.token_bytes(16)
        self.store.kv_set("rbac:pepper", p.hex())
        return p

    def _digest(self, key: str) -> str:
        return self._hasher.hash_text(key + self._pepper.hex())

    # ------------------------------------------------------------- lifecycle
    def create(self, role: str, label: str = "", actor: str = "system",
               ttl_seconds: int | None = None) -> dict:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        secret = secrets.token_hex(16)
        key = f"{_KEY_PREFIX}_{role}_{secret}"
        kid = f"key_{secrets.token_hex(4)}"
        import json
        meta = {"id": kid, "role": role, "label": label,
                "created_by": actor, "created_at": time.time(),
                "digest": self._digest(key),
                "expires_at": (time.time() + ttl_seconds) if ttl_seconds else None,
                "revoked": False}
        self.store.kv_set(f"rbac:key:{kid}", json.dumps(meta))
        # index by digest for O(1) lookup at request time
        self.store.kv_set(f"rbac:dgst:{meta['digest']}", kid)
        return {"key": key, "id": kid, "role": role, "label": label,
                "expires_at": meta["expires_at"]}

    def verify(self, key: str) -> dict | None:
        """Return key metadata iff valid (exists, not revoked, not expired)."""
        import json
        if not key.startswith(f"{_KEY_PREFIX}_"):
            return None
        dgst = self._digest(key)
        kid = self.store.kv_get(f"rbac:dgst:{dgst}")
        if not kid:
            return None
        raw = self.store.kv_get(f"rbac:key:{kid}")
        if not raw:
            return None
        try:
            meta = json.loads(raw)
        except Exception:
            return None
        if meta.get("revoked"):
            return None
        exp = meta.get("expires_at")
        if exp and time.time() > exp:
            return None
        return meta

    def revoke(self, kid: str) -> bool:
        import json
        raw = self.store.kv_get(f"rbac:key:{kid}")
        if not raw:
            return False
        meta = json.loads(raw)
        meta["revoked"] = True
        meta["revoked_at"] = time.time()
        self.store.kv_set(f"rbac:key:{kid}", json.dumps(meta))
        dgst = meta.get("digest")
        if dgst:
            self.store.kv_set(f"rbac:dgst:{dgst}", "")  # break the index
        return True

    def list_keys(self) -> list[dict]:
        import json
        out = []
        for k, v in self.store.iter_kv("rbac:key:"):
            meta = json.loads(v)
            meta.pop("digest", None)  # never leak digests
            out.append(meta)
        return sorted(out, key=lambda m: m.get("created_at", 0))


def authorize(meta: dict | None, action: str) -> dict:
    """Raise RBACError unless ``meta`` (from verify()) may do ``action``."""
    if meta is None:
        raise RBACError(action, "anonymous")
    role = meta.get("role", "reader")
    allowed = PERMISSIONS.get(action)
    if allowed is None:
        raise RBACError(action, role)
    if role not in allowed:
        raise RBACError(action, role)
    return meta


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
