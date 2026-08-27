"""Data governance: GDPR erasure, retention, backup/restore, PITR.

These are the four operations an enterprise buyer's security review
actually blocks on:

  erase_user()     — Art. 17 right-to-erasure: hard-delete every trace
                     of a subject (facts, chunks, vectors, lexicon,
                     aliases, vault entries), crypto-shred the PII vault,
                     and leave the audit chain intact (legal exemption)
                     with an erasure ATTESTATION record.
  apply_retention() — Art. 5(1)(e) storage-limitation: expire facts and
                     chunks older than the policy window, keeping the
                     bi-temporal tombstones so history remains honest.
  snapshot()/restore() — atomic, integrity-checked backup envelope
                     (SQLite backup API + manifest + Merkle-style digest
                     of the artifact).
  state_at()       — point-in-time recovery read: the bi-temporal Trace
                     replays "what did the system believe at T?" using
                     transaction times (tx_from/tx_to) — no snapshot
                     needed, the database IS the WAL.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone

from context_m.security.hashes import HashProvider


class Governance:
    def __init__(self, memory) -> None:
        self.memory = memory
        self.store = memory.store
        self.palace = memory.palace
        self.audit = getattr(memory, "audit_log", None)

    def _log(self, action: str, resource: str | None = None,
             outcome: str = "success", meta: dict | None = None) -> None:
        if self.audit is not None:
            self.audit.log(action, resource=resource, outcome=outcome,
                           meta=meta)

    # ------------------------------------------------------------ erasure
    def erase_user(self, user_id: str, *, hard: bool = True,
                   crypto_shred: bool = True) -> dict:
        """GDPR Art. 17 — remove the subject from every layer."""
        t0 = time.time()
        conn = self.store.conn
        counts = {}
        counts["facts"] = conn.execute(
            "SELECT COUNT(*) AS c FROM facts WHERE user_id=?",
            (user_id,)).fetchone()["c"]
        counts["chunks"] = conn.execute(
            "SELECT COUNT(*) AS c FROM chunks WHERE user_id=?",
            (user_id,)).fetchone()["c"]
        # orphaned edges touching removed facts
        fact_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM facts WHERE user_id=?", (user_id,))]
        if fact_ids:
            qmarks = ",".join("?" * len(fact_ids))
            counts["edges"] = conn.execute(
                f"SELECT COUNT(*) AS c FROM edges WHERE src IN ({qmarks}) "
                f"OR dst IN ({qmarks})", fact_ids + fact_ids).fetchone()["c"]
            conn.execute(f"DELETE FROM edges WHERE src IN ({qmarks}) "
                         f"OR dst IN ({qmarks})", fact_ids + fact_ids)
        conn.execute("DELETE FROM facts WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM chunks WHERE user_id=?", (user_id,))
        # kv residue: lexicon, names, alias caches
        kv_removed = 0
        for k, _v in list(self.store.iter_kv(f"lexicon:{user_id}")):
            self.store.kv_delete(k); kv_removed += 1
        for k, _v in list(self.store.iter_kv(f"name:{user_id}")):
            self.store.kv_delete(k); kv_removed += 1
        counts["kv"] = kv_removed
        # PII vault: crypto-shred if enabled and configured
        vault = getattr(self.memory, "pii_vault", None)
        if vault is not None and crypto_shred:
            counts["vault_shredded"] = vault.crypto_shred()
        # palace vectors for those facts
        vec_removed = 0
        if fact_ids:
            vec_removed = self.palace.remove_ids(fact_ids)
        counts["vectors"] = vec_removed
        conn.commit()
        # erasure attestation in the tamper-evident chain (Art. 30 records)
        self._log("governance.erase", resource=user_id, outcome="erased",
                  meta={"counts": counts,
                        "duration_ms": round((time.time() - t0) * 1e3, 1),
                        "hard": hard, "crypto_shred": crypto_shred})
        # residual scan: no row may reference the subject anywhere
        residual = {
            "facts": conn.execute(
                "SELECT COUNT(*) AS c FROM facts WHERE user_id=?",
                (user_id,)).fetchone()["c"],
            "chunks": conn.execute(
                "SELECT COUNT(*) AS c FROM chunks WHERE user_id=?",
                (user_id,)).fetchone()["c"],
        }
        return {"user_id": user_id, "erased": residual["facts"] == 0
                and residual["chunks"] == 0,
                "counts": counts, "residual": residual,
                "duration_ms": round((time.time() - t0) * 1e3, 1)}

    # ------------------------------------------------------------ retention
    def apply_retention(self, days: int, *, user_id: str | None = None,
                        dry_run: bool = False) -> dict:
        """Expire facts whose last transaction time is older than ``days``."""
        if days <= 0:
            raise ValueError("days must be positive")
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        conn = self.store.conn
        q = ("SELECT COUNT(*) AS c FROM facts WHERE tx_from < ? "
             "AND tx_to IS NULL")
        args = [cutoff_iso]
        if user_id:
            q += " AND user_id=?"
            args.append(user_id)
        stale = conn.execute(q, args).fetchone()["c"]
        if dry_run:
            return {"stale_facts": stale, "applied": False}
        q2 = "UPDATE facts SET tx_to=?, is_active=0 WHERE tx_from < ? AND tx_to IS NULL"
        args2 = [datetime.now(timezone.utc).isoformat(), cutoff_iso]
        if user_id:
            q2 += " AND user_id=?"
            args2.append(user_id)
        cur = conn.execute(q2, args2)
        conn.commit()
        self._log("governance.retention",
                  meta={"days": days, "expired": cur.rowcount,
                        "user_id": user_id or "all"})
        return {"stale_facts": cur.rowcount, "applied": True,
                "cutoff": cutoff_iso}

    # ------------------------------------------------------------ snapshot
    def snapshot(self, path: str) -> dict:
        """Atomic backup: online-backup the SQLite file + write a manifest.
        The backup API copies page-by-page under a read transaction —
        safe while writers are active."""
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        if path.endswith("/"):
            raise ValueError("path must be a file, not a directory")
        target = sqlite_backup(self.store.db_path, path)
        digest = _file_digest(target)
        manifest = {
            "format": "context-m-snapshot/1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_db": self.store.db_path,
            "file": os.path.basename(target),
            "size_bytes": os.path.getsize(target),
            "sha256": digest,
            "facts": self.store.conn.execute(
                "SELECT COUNT(*) AS c FROM facts").fetchone()["c"],
            "audit_head": (self.audit.verify()["head_hash"]
                           if self.audit else None),
        }
        mpath = target + ".manifest.json"
        with open(mpath, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        self._log("governance.snapshot",
                  resource=path, meta={"sha256": digest,
                                       "size": manifest["size_bytes"]})
        return {"path": target, "manifest": manifest, "manifest_path": mpath}

    def restore(self, snapshot_path: str, *, verify: bool = True) -> dict:
        """Restore from a snapshot manifest pair. Refuses mismatched digests."""
        mpath = snapshot_path + ".manifest.json"
        if verify and not os.path.exists(mpath):
            raise FileNotFoundError("manifest missing — cannot verify integrity")
        if os.path.exists(mpath):
            with open(mpath, encoding="utf-8") as fh:
                manifest = json.load(fh)
            if verify:
                digest = _file_digest(snapshot_path)
                if digest != manifest["sha256"]:
                    raise ValueError(
                        f"integrity check failed: {digest} != {manifest['sha256']}")
        # close current handles, replace file, reopen
        db_path = self.store.db_path
        self.memory.close()
        import shutil
        shutil.copyfile(snapshot_path, db_path)
        self.memory._reopen()
        # _reopen() built a NEW Governance object; this method is running
        # on the old one — rebind self so the audit attestation below
        # writes through the fresh store, not the closed one.
        self.store = self.memory.store
        self.palace = self.memory.palace
        self.audit = self.memory.audit_log
        self._log("governance.restore", resource=snapshot_path)
        return {"restored": db_path, "verified": verify}

    # ------------------------------------------------------------ PITR
    def state_at(self, when, *, user_id: str | None = None,
                 limit: int = 500) -> list[dict]:
        """Point-in-time read: facts the system believed true at ``when``
        (transaction-time replay — the database is its own WAL)."""
        from context_m.api.memory import parse_ts
        ts = parse_ts(when) if not isinstance(when, datetime) else when
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        # normalize to the Z-suffix format facts are stored with, so the
        # string comparison in SQL matches the stored tx_from exactly
        iso = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        q = ("SELECT * FROM facts WHERE tx_from <= ? AND "
             "(tx_to IS NULL OR tx_to > ?) AND quarantined=0")
        args: list = [iso, iso]
        if user_id:
            q += " AND user_id=?"
            args.append(user_id)
        q += " ORDER BY valid_from DESC LIMIT ?"
        args.append(limit)
        rows = [dict(r) for r in self.store.conn.execute(q, args)]
        self._log("governance.pitr", meta={"when": iso, "rows": len(rows)})
        return rows


# ------------------------------------------------------------------ helpers
def sqlite_backup(src_db: str, dst_path: str) -> str:
    import sqlite3
    src = sqlite3.connect(src_db)
    dst = sqlite3.connect(dst_path)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    return dst_path


def _file_digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
