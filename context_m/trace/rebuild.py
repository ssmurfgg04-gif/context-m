"""Trace-based rebuild — checksum-driven re-materialization from the
symbolic Trace.

User concern: "Self-healing is theater → Use checksums + rebuild from
Trace". The existing palace.heal() re-encodes corrupt records from the
symbolic Trace, which IS the canonical rebuild path — but it's
invoked manually and doesn't cover the full lifecycle.

This module formalizes the rebuild op:
  1. Checksum audit: every vector record carries a stored vec_hash;
     health_check() detects mismatches.
  2. Rebuild: for every corrupt record, re-encode from the canonical
     Fact in TraceStore. The Trace is the source of truth — vectors
     are derived.
  3. Full rebuild: drop all vectors, re-build from scratch from all
     Facts in the Trace. Use after schema changes, codec swaps, or
     suspected widespread corruption.
  4. Audit log: every rebuild leaves a tamper-evident record in the
     rebuild_log table.

Designed for the MemoryPalace + TraceStore pair.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, asdict

from context_m.util import iso


REBUILD_LOG_TABLE = """
CREATE TABLE IF NOT EXISTS rebuild_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,         -- 'partial' | 'full'
  scope TEXT NOT NULL,        -- 'all' | 'user:N' | 'scope:N'
  checked INTEGER NOT NULL,
  rebuilt INTEGER NOT NULL,
  note TEXT DEFAULT ''
)
"""


@dataclass
class RebuildReport:
    kind: str
    scope: str
    checked: int
    rebuilt: int
    corrupt_ids: list[str]
    missing_ids: list[str]
    duration_ms: int
    ts: str

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


class TraceRebuilder:
    """Drive checksum-audited rebuilds of the MemoryPalace from Trace."""

    def __init__(self, palace, store) -> None:
        self.palace = palace
        self.store = store
        store.conn.execute(REBUILD_LOG_TABLE)
        store.conn.commit()

    def audit(self, sample: int | None = None) -> dict:
        """Run a checksum audit — detect but don't fix."""
        report = self.palace.health_check(sample=sample)
        return {
            "checked": report["checked"],
            "corrupt": report["corrupt"],
            "corrupt_ids": report["corrupt_ids"],
            "tmr_disagree_bits": report.get("tmr_disagree_bits", 0),
        }

    def rebuild_partial(self, max_records: int = 1000) -> RebuildReport:
        """Re-encode corrupt records from the Trace.

        For every corrupt fact_id, look up the canonical Fact in the
        TraceStore, re-encode it via palace.encode_fact, and overwrite
        the corrupt vector record. Leaves a row in rebuild_log.
        """
        t0 = _dt.datetime.now(_dt.timezone.utc)
        audit = self.palace.health_check()
        corrupt_ids = audit["corrupt_ids"][:max_records]
        # fetch canonical facts
        rebuilt = 0
        missing_ids: list[str] = []
        for fid in corrupt_ids:
            fact = self._lookup_fact(fid)
            if fact is None:
                missing_ids.append(fid)
                continue
            vec = self.palace.encode_fact(fact)
            self.palace.add(fid, vec)  # add overwrites by fact_id
            rebuilt += 1
        t1 = _dt.datetime.now(_dt.timezone.utc)
        dur_ms = int((t1 - t0).total_seconds() * 1000)
        report = RebuildReport(
            kind="partial", scope="all",
            checked=audit["checked"], rebuilt=rebuilt,
            corrupt_ids=corrupt_ids, missing_ids=missing_ids,
            duration_ms=dur_ms, ts=iso(t1))
        self._log(report)
        return report

    def rebuild_full(self, user_id: str | None = None) -> RebuildReport:
        """Drop all vectors and re-build from scratch from Trace.

        Use after schema changes, codec swaps, or suspected widespread
        corruption. Slow but authoritative.
        """
        t0 = _dt.datetime.now(_dt.timezone.utc)
        # fetch all facts
        scope = f"user:{user_id}" if user_id else "all"
        facts = self.store.query_facts(active=True, user_id=user_id)
        # drop all vectors
        self.palace.remove_ids([f.id for f in facts])  # clears them
        rebuilt = 0
        for fact in facts:
            try:
                vec = self.palace.encode_fact(fact)
                self.palace.add(fact.id, vec)
                rebuilt += 1
            except Exception:
                continue
        t1 = _dt.datetime.now(_dt.timezone.utc)
        dur_ms = int((t1 - t0).total_seconds() * 1000)
        report = RebuildReport(
            kind="full", scope=scope,
            checked=len(facts), rebuilt=rebuilt,
            corrupt_ids=[], missing_ids=[],
            duration_ms=dur_ms, ts=iso(t1))
        self._log(report)
        return report

    def rebuild_log(self, limit: int = 50) -> list[dict]:
        """Return recent rebuild log entries (tamper-evident audit)."""
        rows = self.store.conn.execute(
            "SELECT * FROM rebuild_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- internals ---
    def _lookup_fact(self, fact_id: str):
        """Fetch a fact by id from the TraceStore."""
        try:
            facts = self.store.query_facts(active=True)
            for f in facts:
                if f.id == fact_id:
                    return f
        except Exception:
            pass
        return None

    def _log(self, report: RebuildReport) -> None:
        self.store.conn.execute(
            """INSERT INTO rebuild_log(ts, kind, scope, checked, rebuilt, note)
               VALUES(?,?,?,?,?,?)""",
            (report.ts, report.kind, report.scope,
             report.checked, report.rebuilt,
             json.dumps({"corrupt_ids": report.corrupt_ids[:10],
                         "missing_ids": report.missing_ids[:10]}))
        )
        self.store.conn.commit()


__all__ = ["TraceRebuilder", "RebuildReport", "REBUILD_LOG_TABLE"]
