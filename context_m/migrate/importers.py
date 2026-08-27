"""Migration importers — Mem0 / Zep / Chroma → Context-M.

The plan's Trojan-Horse migration tooling: read a competitor's local
store, convert to Trace triples (μ=0 re-extraction on raw text where
possible — timestamps preserved), and keep the original ids in
provenance for audit.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

from context_m.errors import MigrationError
from context_m.util import parse_ts


def _iter_mem0_rows(path: str):
    """Yield (text, created_at) from a Mem0 SQLite store.

    Mem0's local store keeps a ``history`` table with JSON payloads
    containing the original conversation plus extracted memories.
    """
    if not os.path.exists(path):
        raise MigrationError(f"mem0 store not found: {path}")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "history" in tables:
            for row in conn.execute(
                    "SELECT data, created_at, updated_at FROM history "
                    "ORDER BY id"):
                raw = row["data"] or ""
                created = row["created_at"] or row["updated_at"]
                try:
                    payload = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    payload = None
                texts: list[str] = []
                if isinstance(payload, dict):
                    for msg in payload.get("messages", []):
                        if isinstance(msg, dict) and msg.get("content"):
                            texts.append(str(msg["content"]))
                    for mem in payload.get("memories", []):
                        if isinstance(mem, str):
                            texts.append(mem)
                elif isinstance(raw, str) and raw.strip():
                    texts.append(raw.strip())
                for t in texts:
                    if t and len(t) > 2:
                        yield t, created
        elif "memories" in tables:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
            text_col = "data" if "data" in cols else (
                "memory" if "memory" in cols else "text")
            for row in conn.execute(f"SELECT {text_col} AS t, created_at "
                                    f"FROM memories ORDER BY rowid"):
                if row["t"]:
                    yield str(row["t"]), row["created_at"]
        else:
            raise MigrationError(
                f"unrecognized mem0 schema (tables: {sorted(tables)})")
    finally:
        conn.close()


def _iter_zep_rows(path: str):
    """Yield from a Zep JSONL export: {"subject","relation","object",
    "valid_at","invalid_at"} or {"text","created_at"} rows."""
    if not os.path.exists(path):
        raise MigrationError(f"zep export not found: {path}")
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield row


def _iter_chroma_rows(path: str):
    """Yield documents from a chroma.sqlite3 store."""
    if not os.path.exists(path):
        raise MigrationError(f"chroma store not found: {path}")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "embeddings" in tables:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(embeddings)")}
            doc_col = None
            for c in ("document", "documents", "content", "data"):
                if c in cols:
                    doc_col = c
                    break
            if doc_col:
                for row in conn.execute(
                        f"SELECT {doc_col} AS doc FROM embeddings"):
                    if row["doc"]:
                        yield str(row["doc"]), None
                return
        if "documents" in tables:
            for row in conn.execute("SELECT * FROM documents"):
                for v in row:
                    if v:
                        yield str(v), None
                return
        raise MigrationError(f"unrecognized chroma schema: {sorted(tables)}")
    finally:
        conn.close()


# ------------------------------------------------------------------
def import_mem0(memory, path: str, user_id: str = "migrated",
                batch: int = 200) -> dict:
    """Import a Mem0 store into Context-M (μ=0 re-extraction)."""
    n_msgs, n_facts = 0, 0
    buf, last_ts = [], None
    for text, created in _iter_mem0_rows(path):
        buf.append(text)
        last_ts = created
        if len(buf) >= batch:
            out = memory.add(buf, user_id=user_id,
                             timestamp=parse_ts(last_ts) or
                             datetime.now(timezone.utc))
            n_msgs += len(buf)
            n_facts += out["stats"]["facts_inserted"]
            buf = []
    if buf:
        out = memory.add(buf, user_id=user_id,
                         timestamp=parse_ts(last_ts) or
                         datetime.now(timezone.utc))
        n_msgs += len(buf)
        n_facts += out["stats"]["facts_inserted"]
    return {"source": "mem0", "messages": n_msgs, "facts": n_facts}


def import_zep(memory, path: str, user_id: str = "migrated") -> dict:
    """Import a Zep JSONL export. Graph triples keep bi-temporal windows;
    raw text rows are re-extracted (μ=0)."""
    from context_m.trace.fact import make_fact
    n_triples, n_texts = 0, 0
    commit = memory.store.create_commit("migrate: zep")
    for row in _iter_zep_rows(path):
        if all(k in row for k in ("subject", "relation", "object")):
            f = make_fact(row["subject"], row["relation"], row["object"],
                          now=parse_ts(row.get("valid_at")) or
                          datetime.now(timezone.utc),
                          valid_to=row.get("invalid_at"),
                          user_id=user_id, confidence=0.8,
                          provenance={"migrated_from": "zep"})
            memory.store.insert_fact(f, commit)
            memory.palace.add(f.id, memory.palace.encode_fact(f))
            n_triples += 1
        elif row.get("text"):
            memory.add(row["text"], user_id=user_id)
            n_texts += 1
    memory.store.end_batch()
    memory.reader.invalidate_caches()
    return {"source": "zep", "triples": n_triples, "texts": n_texts}


def import_chroma(memory, path: str, user_id: str = "migrated",
                  batch: int = 200) -> dict:
    """Import documents from a Chroma store (μ=0 re-extraction; foreign
    embeddings are NOT portable across embedding models)."""
    n_msgs, n_facts = 0, 0
    buf = []
    for doc, _ts in _iter_chroma_rows(path):
        buf.append(doc)
        if len(buf) >= batch:
            out = memory.add(buf, user_id=user_id)
            n_msgs += len(buf)
            n_facts += out["stats"]["facts_inserted"]
            buf = []
    if buf:
        out = memory.add(buf, user_id=user_id)
        n_msgs += len(buf)
        n_facts += out["stats"]["facts_inserted"]
    return {"source": "chroma", "documents": n_msgs, "facts": n_facts}


MIGRATORS = {"mem0": import_mem0, "zep": import_zep, "chroma": import_chroma}
