"""Hash-chained, append-only audit log (SOC 2 / SIEM ready).

Every security-relevant operation — add, search, delete, erasure, key
management, snapshot, restore — appends one record:

  {seq, ts, actor, role, action, resource, outcome, meta,
   prev_hash, hash}

``hash = BLAKE2b(prev_hash || canonical-record)`` — tampering with any
record breaks the chain and ``verify()`` pinpoints the first damaged
sequence number. Records are also exported as JSONL (one per line) for
Splunk / Elastic / Datadog ingestion, and a syslog-style single-line
format for legacy collectors.

GDPR note: the audit chain is intentionally exempt from user erasure
(accounting/legitimate-interest records). It stores actor + resource
ids, never raw conversation text.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from context_m.security.hashes import HashProvider

# actions that are always audited
AUDITED_ACTIONS = {
    "memory.add", "memory.search", "memory.update", "memory.delete",
    "memory.delete_all", "memory.verify",
    "governance.erase", "governance.retention", "governance.snapshot",
    "governance.restore", "governance.pitr",
    "keys.create", "keys.revoke", "auth.failure", "security.quarantine",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class AuditLog:
    def __init__(self, store, enabled: bool = True) -> None:
        self.store = store
        self.enabled = enabled
        self._hasher = HashProvider("blake2b")
        self._lock = threading.Lock()
        self._ensure_table()

    def _ensure_table(self) -> None:
        self.store.conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                actor TEXT NOT NULL,
                role TEXT,
                action TEXT NOT NULL,
                resource TEXT,
                outcome TEXT NOT NULL,
                meta TEXT,
                prev_hash TEXT NOT NULL,
                hash TEXT NOT NULL
            )""")
        self.store.conn.commit()

    # ------------------------------------------------------------- append
    def log(self, action: str, *, actor: str = "system", role: str | None = None,
            resource: str | None = None, outcome: str = "success",
            meta: dict | None = None) -> dict | None:
        if not self.enabled:
            return None
        with self._lock:
            row = self.store.conn.execute(
                "SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev = row["hash"] if row else "genesis"
            rec = {
                "ts": _now_iso(), "actor": actor, "role": role or "-",
                "action": action, "resource": resource or "-",
                "outcome": outcome,
                "meta": json.dumps(meta or {}, sort_keys=True),
            }
            canonical = json.dumps(rec, sort_keys=True, separators=(",", ":"))
            digest = self._hasher.hash_text(f"{prev}|{canonical}")
            self.store.conn.execute(
                "INSERT INTO audit_log(ts, actor, role, action, resource,"
                " outcome, meta, prev_hash, hash) VALUES(?,?,?,?,?,?,?,?,?)",
                (rec["ts"], rec["actor"], rec["role"], rec["action"],
                 rec["resource"], rec["outcome"], rec["meta"], prev, digest))
            self.store.conn.commit()
            return {"seq": self.store.conn.execute(
                "SELECT last_insert_rowid() AS s").fetchone()["s"],
                **rec, "prev_hash": prev, "hash": digest}

    # ------------------------------------------------------------- read
    def tail(self, n: int = 100, actor: str | None = None,
             action: str | None = None) -> list[dict]:
        q = "SELECT * FROM audit_log"
        conds, args = [], []
        if actor:
            conds.append("actor=?"); args.append(actor)
        if action:
            conds.append("action=?"); args.append(action)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY seq DESC LIMIT ?"
        args.append(int(n))
        return [dict(r) for r in self.store.conn.execute(q, args)]

    def verify(self) -> dict:
        """Recompute the chain; report the first broken seq."""
        prev = "genesis"
        broken_at = None
        n = 0
        for row in self.store.conn.execute(
                "SELECT * FROM audit_log ORDER BY seq ASC"):
            n += 1
            rec = {"ts": row["ts"], "actor": row["actor"], "role": row["role"],
                   "action": row["action"], "resource": row["resource"],
                   "outcome": row["outcome"], "meta": row["meta"]}
            canonical = json.dumps(rec, sort_keys=True, separators=(",", ":"))
            digest = self._hasher.hash_text(f"{prev}|{canonical}")
            if digest != row["hash"] or prev != row["prev_hash"]:
                broken_at = row["seq"]
                break
            prev = row["hash"]
        return {"records": n, "intact": broken_at is None,
                "first_broken_seq": broken_at,
                "head_hash": prev if broken_at is None else None}

    # ------------------------------------------------------------- export
    def export_jsonl(self, path: str) -> int:
        """SIEM ingestion export (Splunk/Elastic/Datadog friendly)."""
        count = 0
        with open(path, "w", encoding="utf-8") as fh:
            for row in self.store.conn.execute(
                    "SELECT * FROM audit_log ORDER BY seq ASC"):
                rec = {k: row[k] for k in row.keys()}
                try:
                    rec["meta"] = json.loads(rec["meta"])
                except Exception:
                    pass
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
                count += 1
        return count

    def export_syslog(self, path: str) -> int:
        """RFC-3164-style lines: <134>ts actor action resource outcome."""
        count = 0
        with open(path, "w", encoding="utf-8") as fh:
            for row in self.store.conn.execute(
                    "SELECT * FROM audit_log ORDER BY seq ASC"):
                ts = row["ts"][:19].replace("T", " ")
                fh.write(f"<134>{ts} context-m audit: actor={row['actor']} "
                         f"action={row['action']} resource={row['resource']} "
                         f"outcome={row['outcome']} seq={row['seq']}\n")
                count += 1
        return count


class AuditContext:
    """Request-scoped audit binding (actor/role propagated by the server)."""

    def __init__(self, audit: AuditLog | None, actor: str = "system",
                 role: str | None = None) -> None:
        self.audit = audit
        self.actor = actor
        self.role = role

    def log(self, action: str, **kw) -> None:
        if self.audit is not None:
            self.audit.log(action, actor=self.actor, role=self.role, **kw)
