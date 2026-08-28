"""The Symbolic Trace — bi-temporal fact store on SQLite (WAL).

Implements the Section 1.1 data model: Subject-Relation-Value triples
with valid/transaction time, contradiction + temporal + provenance
edges, source chunks, Memory-Git commits (hash-chained) and branches.
SQLite is the embedded substrate for local/edge operation; the store is
designed so an ArcadeDB backend can replace it behind the same API
(the plan's production graph engine).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

from cortexm.errors import BranchError, StoreError
from cortexm.security.hashes import HashProvider
from cortexm.trace.fact import Fact
from cortexm.util import iso, new_id, parse_ts, token_estimate

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
  id TEXT PRIMARY KEY,
  subject TEXT NOT NULL, relation TEXT NOT NULL, value TEXT NOT NULL,
  valid_from TEXT NOT NULL, valid_to TEXT,
  tx_from TEXT NOT NULL, tx_to TEXT,
  confidence REAL DEFAULT 0.8,
  source_hash TEXT DEFAULT '', source_id TEXT DEFAULT '',
  user_id TEXT DEFAULT 'default', agent_id TEXT, run_id TEXT,
  memory_type TEXT DEFAULT 'short_term',
  access_count INTEGER DEFAULT 0, reinforcement INTEGER DEFAULT 1,
  is_active INTEGER DEFAULT 1, is_derived INTEGER DEFAULT 0,
  quarantined INTEGER DEFAULT 0,
  birth_commit TEXT, retired_commit TEXT,
  provenance TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_facts_sr ON facts(subject, relation);
CREATE INDEX IF NOT EXISTS idx_facts_user ON facts(user_id);
CREATE INDEX IF NOT EXISTS idx_facts_active ON facts(is_active);
CREATE INDEX IF NOT EXISTS idx_facts_rel ON facts(relation);
CREATE INDEX IF NOT EXISTS idx_facts_valid ON facts(valid_from);
CREATE INDEX IF NOT EXISTS idx_facts_birth ON facts(birth_commit);
-- composite indexes added for the v2 SPARQL + REST query paths
CREATE INDEX IF NOT EXISTS idx_facts_user_active ON facts(user_id, is_active);
CREATE INDEX IF NOT EXISTS idx_facts_value ON facts(value);
CREATE INDEX IF NOT EXISTS idx_facts_subject_value ON facts(subject, value);

CREATE TABLE IF NOT EXISTS edges (
  src TEXT NOT NULL, dst TEXT NOT NULL, kind TEXT NOT NULL,
  meta TEXT DEFAULT '{}', created TEXT,
  PRIMARY KEY (src, dst, kind)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
-- kind index — critical for SPARQL `?a edge:CAUSAL ?b` queries that
-- post-filter on the kind column
CREATE INDEX IF NOT EXISTS idx_edges_kind ON edges(kind);

CREATE TABLE IF NOT EXISTS chunks (
  id TEXT PRIMARY KEY, text TEXT NOT NULL,
  user_id TEXT, agent_id TEXT, run_id TEXT,
  ts TEXT, source TEXT DEFAULT '', hash TEXT DEFAULT '', tokens INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chunks_user ON chunks(user_id);

CREATE TABLE IF NOT EXISTS commits (
  id TEXT PRIMARY KEY, parents TEXT DEFAULT '[]', branch TEXT,
  message TEXT DEFAULT '', ts TEXT, chain_hash TEXT, n_facts INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS branches (
  name TEXT PRIMARY KEY, head TEXT NOT NULL, created TEXT
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

FACT_COLUMNS = ("id, subject, relation, value, valid_from, valid_to, tx_from, tx_to, "
                "confidence, source_hash, source_id, user_id, agent_id, run_id, "
                "memory_type, access_count, reinforcement, is_active, is_derived, "
                "quarantined, birth_commit, retired_commit, provenance")


class _SafeCursor:
    """Cursor-like object over eagerly-materialized rows (thread-safe)."""

    def __init__(self, rows, rowcount, lastrowid, description) -> None:
        self._rows = rows
        self._iter = iter(rows)
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self.description = description
        self.arraysize = 1

    def fetchone(self):
        try:
            return next(self._iter)
        except StopIteration:
            return None

    def fetchall(self):
        rest = list(self._iter)
        self._iter = iter([])
        return rest

    def fetchmany(self, size=None):
        out = []
        for _ in range(size or 1):
            try:
                out.append(next(self._iter))
            except StopIteration:
                break
        return out

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._iter)

    def close(self) -> None:
        pass


class SafeConnection:
    """Serializes every statement on one SQLite connection.

    SQLite connections are not safe for concurrent cursor use even with
    ``check_same_thread=False`` — interleaved commit/iterate produces
    InterfaceError('bad parameter or other API misuse'). This wrapper
    holds an RLock across execute+materialize and across commit, making
    the whole TraceStore safe under multi-threaded load (REST server,
    concurrent writers). Eager materialization keeps semantics: callers
    only use fetchone/fetchall/iteration/rowcount/lastrowid.
    """

    def __init__(self, conn) -> None:
        self._conn = conn
        self._lock = threading.RLock()

    def execute(self, sql, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            try:
                rows = cur.fetchall()
            except Exception:
                rows = []
            rc, lrid, desc = cur.rowcount, cur.lastrowid, cur.description
            cur.close()
            return _SafeCursor(rows, rc, lrid, desc)

    def executemany(self, sql, seq):
        with self._lock:
            cur = self._conn.executemany(sql, seq)
            rc = cur.rowcount
            cur.close()
            return _SafeCursor([], rc, None, None)

    def executescript(self, script):
        with self._lock:
            return self._conn.executescript(script)

    def commit(self):
        with self._lock:
            self._conn.commit()

    def rollback(self):
        with self._lock:
            self._conn.rollback()

    def close(self):
        with self._lock:
            self._conn.close()

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, v):
        with self._lock:
            self._conn.row_factory = v

    @property
    def in_transaction(self):
        return self._conn.in_transaction


class TraceStore:
    def __init__(self, db_path: str = ":memory:", provider: HashProvider | None = None,
                 wal_sync: str = "normal") -> None:
        self.db_path = db_path
        self.hasher = provider or HashProvider()
        mem = db_path in (":memory:", None, "")
        self.conn = SafeConnection(
            sqlite3.connect(db_path or ":memory:", check_same_thread=False))
        if not mem:
            # Aeon-inspired crash-recoverable write path:
            #   journal_mode=WAL — readers never block the writer, and a
            #                       torn write rolls back cleanly on reopen.
            #   synchronous      — NORMAL: commits survive process crash
            #                       (SIGKILL) at full speed; FULL additionally
            #                       survives OS/power loss at fsync cost.
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute(
                "PRAGMA synchronous="
                + ("FULL" if str(wal_sync).lower() == "full" else "NORMAL"))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._ancestry_cache: dict[str, frozenset[str]] = {}
        self._active_cache: dict[str, frozenset[str]] = {}
        self._batching = False
        self._ensure_genesis()

    def checkpoint(self, mode: str = "TRUNCATE") -> None:
        """Fold the WAL back into the main db file (shrink + fast reopen)."""
        if self.db_path in (":memory:", None, ""):
            return
        try:
            self.conn.execute(f"PRAGMA wal_checkpoint({mode})")
        except Exception:
            pass

    def begin_batch(self) -> None:
        self._batching = True

    def end_batch(self) -> None:
        self._batching = False
        self.conn.commit()

    def _maybe_commit(self) -> None:
        if not self._batching:
            self.conn.commit()

    @property
    def batching(self) -> bool:
        return self._batching

    # ------------------------------------------------------------------ kv
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, value))
        self.conn.commit()

    def iter_kv(self, prefix: str = ""):
        """Yield (key, value) pairs whose key starts with ``prefix``."""
        cur = self.conn.execute(
            "SELECT k, v FROM kv WHERE k LIKE ? ORDER BY k",
            (prefix + "%",))
        for row in cur:
            yield row["k"], row["v"]

    def kv_delete(self, key: str) -> None:
        self.conn.execute("DELETE FROM kv WHERE k=?", (key,))
        self.conn.commit()

    # -------------------------------------------------------------- genesis
    def _ensure_genesis(self) -> None:
        if not self.conn.execute("SELECT 1 FROM branches LIMIT 1").fetchone():
            cid = new_id()
            now = iso(datetime.utcnow().__class__.now() if False else datetime.utcnow()) if False else iso(datetime.now())  # noqa
            chain = self.hasher.hash_text("genesis:" + cid)
            self.conn.execute(
                "INSERT INTO commits(id, parents, branch, message, ts, chain_hash) VALUES(?,?,?,?,?,?)",
                (cid, "[]", "main", "genesis", now, chain))
            self.conn.execute(
                "INSERT INTO branches(name, head, created) VALUES(?,?,?)", ("main", cid, now))
            self.kv_set("HEAD_BRANCH", "main")
            self.kv_set("SCHEMA_VERSION", "1")

    # -------------------------------------------------------------- chunks
    def add_chunk(self, text: str, *, user_id: str = "default", agent_id: str | None = None,
                  run_id: str | None = None, ts: datetime | str | None = None,
                  source: str = "", chunk_id: str | None = None) -> str:
        cid = chunk_id or new_id()
        ts_s = iso(parse_ts(ts) or datetime.now(timezone.utc)) if ts else iso(datetime.now(timezone.utc))
        self.conn.execute(
            "INSERT OR REPLACE INTO chunks(id, text, user_id, agent_id, run_id, ts, source, hash, tokens) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (cid, text, user_id, agent_id, run_id, ts_s, source,
             self.hasher.hash_text(text), token_estimate(text)))
        self._maybe_commit()
        return cid

    def get_chunk(self, chunk_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM chunks WHERE id=?", (chunk_id,)).fetchone()
        return dict(row) if row else None

    def all_chunks(self, user_id: str | None = None) -> list[dict]:
        if user_id:
            rows = self.conn.execute("SELECT * FROM chunks WHERE user_id=? ORDER BY ts", (user_id,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM chunks ORDER BY ts").fetchall()
        return [dict(r) for r in rows]

    def quarantined_chunk_texts(self, user_id: str | None = None) -> list[str]:
        """Source texts of every quarantined fact (the tainted corpus used
        by the MINJA contagion guard on the write path)."""
        sql = ("SELECT DISTINCT c.text FROM chunks c "
               "JOIN facts f ON f.source_id = c.id WHERE f.quarantined = 1")
        args: tuple = ()
        if user_id is not None:
            sql += " AND c.user_id = ?"
            args = (user_id,)
        return [r[0] for r in self.conn.execute(sql, args).fetchall()]

    # ------------------------------------------------------------- commits
    def create_commit(self, message: str = "", branch: str | None = None,
                      parents: list[str] | None = None, n_facts: int = 0) -> str:
        branch = branch or self.current_branch()
        head = self.head(branch)
        parents = parents if parents is not None else ([head] if head else [])
        cid = new_id()
        now = iso(datetime.now(timezone.utc))
        parent_chains = [self.conn.execute(
            "SELECT chain_hash FROM commits WHERE id=?", (p,)).fetchone() for p in parents]
        chain = self.hasher.hash_json({
            "commit": cid, "parents": parents, "message": message,
            "ts": now,
            "parent_chains": [r["chain_hash"] if r else "" for r in parent_chains],
        })
        self.conn.execute(
            "INSERT INTO commits(id, parents, branch, message, ts, chain_hash, n_facts) VALUES(?,?,?,?,?,?,?)",
            (cid, json.dumps(parents), branch, message, now, chain, n_facts))
        if not parents:
            self.conn.execute(
                "INSERT OR REPLACE INTO branches(name, head, created) VALUES(?,?,?)",
                (branch, cid, now))
        else:
            self.conn.execute("UPDATE branches SET head=? WHERE name=?", (cid, branch))
        self._invalidate(branch)
        self._maybe_commit()
        return cid

    def head(self, branch: str | None = None) -> str | None:
        branch = branch or self.current_branch()
        row = self.conn.execute("SELECT head FROM branches WHERE name=?", (branch,)).fetchone()
        return row["head"] if row else None

    def current_branch(self) -> str:
        return self.kv_get("HEAD_BRANCH", "main") or "main"

    def checkout(self, branch: str) -> None:
        if not self.conn.execute("SELECT 1 FROM branches WHERE name=?", (branch,)).fetchone():
            raise BranchError(f"unknown branch {branch!r}")
        self.kv_set("HEAD_BRANCH", branch)

    def create_branch(self, name: str, from_commit: str | None = None,
                      switch: bool = True) -> str:
        if self.conn.execute("SELECT 1 FROM branches WHERE name=?", (name,)).fetchone():
            raise BranchError(f"branch {name!r} already exists")
        base = from_commit or self.head() or self.head(self.current_branch())
        if base is None:
            raise BranchError("cannot branch from empty history")
        self.conn.execute(
            "INSERT INTO branches(name, head, created) VALUES(?,?,?)",
            (name, base, iso(datetime.now(timezone.utc))))
        if switch:
            self.kv_set("HEAD_BRANCH", name)
        self._maybe_commit()
        return base

    def branches(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT b.name, b.head, b.created, c.ts AS head_ts, c.message "
            "FROM branches b LEFT JOIN commits c ON c.id=b.head ORDER BY b.created").fetchall()
        return [dict(r) for r in rows]

    def log(self, branch: str | None = None, limit: int = 50) -> list[dict]:
        cid = self.head(branch or self.current_branch())
        out: list[dict] = []
        seen = set()
        queue = [cid] if cid else []
        while queue and len(out) < limit:
            cur = queue.pop(0)
            if not cur or cur in seen:
                continue
            seen.add(cur)
            row = self.conn.execute("SELECT * FROM commits WHERE id=?", (cur,)).fetchone()
            if not row:
                continue
            out.append(dict(row))
            queue.extend(json.loads(row["parents"]))
        return out

    def ancestry(self, commit_id: str) -> frozenset[str]:
        cached = self._ancestry_cache.get(commit_id)
        if cached is not None:
            return cached
        seen: set[str] = set()
        queue = [commit_id]
        while queue:
            cur = queue.pop()
            if cur in seen:
                continue
            seen.add(cur)
            row = self.conn.execute("SELECT parents FROM commits WHERE id=?", (cur,)).fetchone()
            if row:
                queue.extend(json.loads(row["parents"]))
        result = frozenset(seen)
        if len(self._ancestry_cache) < 64:
            self._ancestry_cache[commit_id] = result
        return result

    def commit(self, commit_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM commits WHERE id=?", (commit_id,)).fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------------- facts
    def insert_fact(self, fact: Fact, commit_id: str | None = None) -> Fact:
        if commit_id:
            fact.birth_commit = commit_id
        row = fact.to_row()
        cols = ", ".join(row.keys())
        ph = ", ".join("?" for _ in row)
        self.conn.execute(f"INSERT INTO facts({cols}) VALUES({ph})", tuple(row.values()))
        return fact

    def insert_facts_bulk(self, facts: list[Fact], commit_id: str | None = None) -> int:
        for f in facts:
            if commit_id:
                f.birth_commit = commit_id
        rows = [f.to_row() for f in facts]
        if not rows:
            return 0
        cols = ", ".join(rows[0].keys())
        ph = ", ".join("?" for _ in rows[0])
        self.conn.executemany(f"INSERT INTO facts({cols}) VALUES({ph})",
                              [tuple(r.values()) for r in rows])
        return len(rows)

    def update_commit_n_facts(self, commit_id: str, n_facts: int) -> None:
        """Update the n_facts counter on a commit (after cognition engine
        appends derived facts post-creation)."""
        if not commit_id:
            return
        self.conn.execute(
            "UPDATE commits SET n_facts = n_facts + ? WHERE id=?",
            (n_facts, commit_id))
        self._maybe_commit()

    def get_fact(self, fact_id: str) -> Fact | None:
        row = self.conn.execute(f"SELECT {FACT_COLUMNS} FROM facts WHERE id=?", (fact_id,)).fetchone()
        return Fact.from_row(dict(row)) if row else None

    def get_facts(self, ids: list[str]) -> list[Fact]:
        if not ids:
            return []
        out = []
        for i in range(0, len(ids), 500):
            batch = ids[i:i + 500]
            q = f"SELECT {FACT_COLUMNS} FROM facts WHERE id IN ({','.join('?' * len(batch))})"
            out.extend(Fact.from_row(dict(r)) for r in self.conn.execute(q, batch))
        return out

    def update_fact(self, fact_id: str, **fields) -> None:
        if not fields:
            return
        if "provenance" in fields and isinstance(fields["provenance"], dict):
            fields["provenance"] = json.dumps(fields["provenance"], default=str)
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE facts SET {sets} WHERE id=?", (*fields.values(), fact_id))
        self._maybe_commit()

    def bump_access(self, fact_ids: list[str]) -> None:
        for fid in fact_ids:
            self.conn.execute("UPDATE facts SET access_count=access_count+1 WHERE id=?", (fid,))
        self._maybe_commit()

    def _fact_filters(self, user_id=None, agent_id=None, run_id=None, branch=None,
                      active=True, include_quarantined=False, subject=None,
                      relation=None, value=None, derived=None):
        clauses, params = [], []
        if active:
            clauses.append("is_active=1")
        if not include_quarantined:
            clauses.append("quarantined=0")
        if user_id is not None:
            clauses.append("user_id=?"); params.append(user_id)
        if agent_id is not None:
            clauses.append("agent_id=?"); params.append(agent_id)
        if run_id is not None:
            clauses.append("run_id=?"); params.append(run_id)
        if subject is not None:
            clauses.append("subject=?"); params.append(subject)
        if relation is not None:
            clauses.append("relation=?"); params.append(relation)
        if value is not None:
            clauses.append("value=?"); params.append(value)
        if derived is not None:
            clauses.append("is_derived=?"); params.append(int(derived))
        if branch is not None:
            ids = self.active_ids(branch)
            if not ids:
                clauses.append("1=0")
            else:
                # membership filtering applied post-hoc for large sets
                pass
        return clauses, params, (branch if branch is not None else None)

    def query_facts(self, *, subject=None, relation=None, value=None, user_id=None,
                    agent_id=None, run_id=None, branch=None, active=True,
                    include_quarantined=False, derived=None, order="valid_from",
                    limit=None) -> list[Fact]:
        clauses, params, branch_filter = self._fact_filters(
            user_id, agent_id, run_id, branch, active, include_quarantined,
            subject, relation, value, derived)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        q = f"SELECT {FACT_COLUMNS} FROM facts{where} ORDER BY {order}"
        if limit:
            q += f" LIMIT {int(limit)}"
        rows = self.conn.execute(q, params).fetchall()
        facts = [Fact.from_row(dict(r)) for r in rows]
        if branch_filter is not None:
            ids = self.active_ids(branch_filter)
            facts = [f for f in facts if f.id in ids]
        return facts

    def active_facts(self, **kw) -> list[Fact]:
        return self.query_facts(**kw)

    def history_of(self, subject: str, relation: str, user_id: str | None = None,
                   include_inactive=True) -> list[Fact]:
        clauses, params, _ = self._fact_filters(
            user_id=user_id, active=not include_inactive)
        extra = "subject=? AND relation=?" + ((" AND " + " AND ".join(clauses)) if clauses else "")
        q = f"SELECT {FACT_COLUMNS} FROM facts WHERE {extra} ORDER BY valid_from, tx_from"
        rows = self.conn.execute(q, (subject, relation, *params)).fetchall()
        return [Fact.from_row(dict(r)) for r in rows]

    def facts_about(self, entity: str, user_id: str | None = None,
                    active: bool = True) -> list[Fact]:
        """Facts where entity is subject OR value (1-hop associative recall)."""
        clauses, params, _ = self._fact_filters(user_id=user_id, active=active)
        extra = "(subject=? OR value=?)" + ((" AND " + " AND ".join(clauses)) if clauses else "")
        q = f"SELECT {FACT_COLUMNS} FROM facts WHERE {extra}"
        rows = self.conn.execute(q, (entity, entity, *params)).fetchall()
        return [Fact.from_row(dict(r)) for r in rows]

    def temporal_window(self, start: str | None, end: str | None,
                        user_id: str | None = None, field: str = "valid",
                        active: bool = True) -> list[Fact]:
        """Zep-compatible temporal queries. field: 'valid' (reality) or 'tx' (recorded).

        Valid-time uses interval-overlap semantics: a fact matches if its
        [valid_from, valid_to] window intersects [start, end] — so asking
        "where did Alice work in 2025?" retrieves an employment that began
        in 2024 and ended in 2026. Transaction-time uses point semantics.
        """
        clauses, fparams, _ = self._fact_filters(user_id=user_id, active=active)
        cond: list[str] = []
        cparams: list = []
        if field == "valid":
            if start:
                cond.append("(valid_to IS NULL OR valid_to>=?)")
                cparams.append(start[:10])
            if end:
                cond.append("valid_from<=?")
                cparams.append(end[:10])
            order = "valid_from"
        else:
            if start:
                cond.append("tx_from>=?"); cparams.append(start[:10])
            if end:
                cond.append("tx_from<=?"); cparams.append(end[:10])
            order = "tx_from"
        params = cparams + fparams
        where = " AND ".join(cond + clauses) if (cond + clauses) else ""
        q = f"SELECT {FACT_COLUMNS} FROM facts{' WHERE ' + where if where else ''} ORDER BY {order}"
        rows = self.conn.execute(q, params).fetchall()
        return [Fact.from_row(dict(r)) for r in rows]

    def count_facts(self, user_id: str | None = None, active_only: bool = True) -> int:
        clauses, params, _ = self._fact_filters(user_id=user_id, active=active_only)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        row = self.conn.execute(f"SELECT COUNT(*) c FROM facts{where}", params).fetchone()
        return int(row["c"])

    # ---------------------------------------------------------- active set
    def active_ids(self, branch: str) -> frozenset[str]:
        cached = self._active_cache.get(branch)
        if cached is not None:
            return cached
        head = self.head(branch)
        if head is None:
            return frozenset()
        anc = self.ancestry(head)
        rows = self.conn.execute(
            "SELECT id, birth_commit, retired_commit FROM facts "
            "WHERE birth_commit IS NOT NULL").fetchall()
        ids = frozenset(
            r["id"] for r in rows
            if r["birth_commit"] in anc and (
                not r["retired_commit"] or r["retired_commit"] not in anc))
        if len(self._active_cache) < 16:
            self._active_cache[branch] = ids
        return ids

    def _invalidate(self, branch: str | None = None) -> None:
        if branch:
            self._active_cache.pop(branch, None)
        else:
            self._active_cache.clear()
        self._ancestry_cache.clear()

    # ---------------------------------------------------------------- edges
    def add_edge(self, src: str, dst: str, kind: str, meta: dict | None = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO edges(src, dst, kind, meta, created) VALUES(?,?,?,?,?)",
            (src, dst, kind, json.dumps(meta or {}), iso(datetime.now(timezone.utc))))
        self._maybe_commit()

    def edges_of(self, fact_id: str, kind: str | None = None, direction: str = "out") -> list[dict]:
        outs, ins = [], []
        if direction in ("out", "both"):
            q = "SELECT * FROM edges WHERE src=?" + (" AND kind=?" if kind else "")
            rows = self.conn.execute(q, (fact_id, kind) if kind else (fact_id,)).fetchall()
            outs = [dict(r, dir="out") for r in rows]
        if direction in ("in", "both"):
            q = "SELECT * FROM edges WHERE dst=?" + (" AND kind=?" if kind else "")
            rows = self.conn.execute(q, (fact_id, kind) if kind else (fact_id,)).fetchall()
            ins = [dict(r, dir="in") for r in rows]
        return outs + ins

    def edges_of_many(self, fact_ids: list[str], kind: str | None = None) -> list[dict]:
        """Edges among a set of fact ids (both directions), batched."""
        if not fact_ids:
            return []
        out: list[dict] = []
        B = 200
        for i in range(0, len(fact_ids), B):
            chunk = fact_ids[i:i + B]
            qm = ",".join("?" * len(chunk))
            q = (f"SELECT * FROM edges WHERE src IN ({qm}) AND dst IN ({qm})"
                 + (f" AND kind=?" if kind else ""))
            rows = self.conn.execute(q, chunk + chunk + ([kind] if kind else []))
            out.extend(dict(r) for r in rows)
        return out

    # ------------------------------------------------------------ integrity
    def active_fact_hashes(self, user_id: str | None = None) -> list[tuple[str, str]]:
        clauses, params, _ = self._fact_filters(user_id=user_id, active=True)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.conn.execute(
            f"SELECT id, source_hash FROM facts{where} ORDER BY id", params).fetchall()
        return [(r["id"], r["source_hash"] or "") for r in rows]

    def stats(self) -> dict:
        def one(q, *p):
            return int(self.conn.execute(q, p).fetchone()[0])
        return {
            "facts": one("SELECT COUNT(*) FROM facts"),
            "active_facts": one("SELECT COUNT(*) FROM facts WHERE is_active=1 AND quarantined=0"),
            "quarantined": one("SELECT COUNT(*) FROM facts WHERE quarantined=1"),
            "derived": one("SELECT COUNT(*) FROM facts WHERE is_derived=1"),
            "chunks": one("SELECT COUNT(*) FROM chunks"),
            "edges": one("SELECT COUNT(*) FROM edges"),
            "commits": one("SELECT COUNT(*) FROM commits"),
            "branches": one("SELECT COUNT(*) FROM branches"),
            "long_term": one("SELECT COUNT(*) FROM facts WHERE memory_type='long_term'"),
        }

    def close(self) -> None:
        try:
            self.conn.commit()
            self.checkpoint("TRUNCATE")
            self.conn.close()
        except Exception:
            pass

    def __enter__(self) -> "TraceStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
