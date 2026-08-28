"""End-to-end verification of the Trojan-Horse migration CLIs.

The plan promises turnkey one-command migration off the incumbents:
    cortexm migrate --from mem0  --path mem0.db
    cortexm migrate --from zep   --path zep.jsonl
    cortexm migrate --from chroma --path chroma.sqlite3
These tests build REALISTIC fixture stores in each vendor's actual on-disk
format, run the importers, and verify facts land and are retrievable.
"""

import datetime as dt
import json
import sqlite3

from context_m import Memory
from context_m.config import Config
from context_m.migrate.importers import (MIGRATORS, import_chroma,
                                         import_mem0, import_zep)

TS = dt.datetime(2026, 1, 10, tzinfo=dt.timezone.utc)


def _mk():
    return Memory(Config())


# ------------------------------------------------------------------ mem0
def test_migrate_mem0_history_schema(tmp_path):
    """Mem0's local store: history table with JSON payloads containing
    the original conversation plus extracted memories."""
    db = str(tmp_path / "mem0.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, data TEXT, "
                 "created_at TEXT, updated_at TEXT)")
    conn.execute(
        "INSERT INTO history(data, created_at, updated_at) VALUES(?,?,?)",
        (json.dumps({
            "messages": [
                {"role": "user", "content": "My name is Alice Chen."},
                {"role": "assistant", "content": "Nice to meet you, Alice!"},
                {"role": "user",
                 "content": "I work at Stripe as a payments engineer."}],
            "memories": ["Name is Alice Chen",
                         "Works at Stripe as payments engineer"],
        }), "2026-01-05T10:00:00", "2026-01-05T10:00:05"))
    conn.execute(
        "INSERT INTO history(data, created_at, updated_at) VALUES(?,?,?)",
        (json.dumps({
            "messages": [
                {"role": "user", "content": "I moved to Lisbon in March."}],
            "memories": ["Lives in Lisbon"],
        }), "2026-03-02T09:00:00", "2026-03-02T09:00:02"))
    conn.commit()
    conn.close()

    m = _mk()
    out = import_mem0(m, db, user_id="alice")
    assert out["messages"] >= 5
    assert out["facts"] >= 3
    view = m.search("Where does Alice work?", user_id="alice")
    assert "stripe" in view["context_block"].lower()
    view = m.search("Where does Alice live?", user_id="alice")
    assert "lisbon" in view["context_block"].lower()
    m.close()


def test_migrate_mem0_memories_schema(tmp_path):
    """Alternative mem0 layout: bare memories table."""
    db = str(tmp_path / "mem0b.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, memory TEXT,"
                 " created_at TEXT)")
    for txt in ("User prefers oat milk lattes.",
                "User knows Rust.",
                "User's manager is Priya."):
        conn.execute("INSERT INTO memories(memory, created_at) VALUES(?,?)",
                     (txt, "2026-02-01T00:00:00"))
    conn.commit()
    conn.close()

    m = _mk()
    out = import_mem0(m, db, user_id="bob")
    assert out["messages"] == 3
    view = m.search("What does Bob prefer to drink?", user_id="bob")
    assert "oat milk" in view["context_block"].lower()
    m.close()


def test_migrate_mem0_missing_store(tmp_path):
    m = _mk()
    import pytest
    from context_m.errors import MigrationError
    with pytest.raises(MigrationError):
        import_mem0(m, str(tmp_path / "nope.db"))
    m.close()


# ------------------------------------------------------------------ zep
def test_migrate_zep_graph_and_text(tmp_path):
    """Zep JSONL export: temporal graph triples + raw text rows."""
    path = str(tmp_path / "zep.jsonl")
    rows = [
        {"subject": "Carlos", "relation": "works_at", "object": "Netflix",
         "valid_at": "2025-06-01", "invalid_at": None},
        {"subject": "Carlos", "relation": "works_at", "object": "Vercel",
         "valid_at": "2026-01-15", "invalid_at": None},
        {"subject": "Carlos", "relation": "lives_in", "object": "Madrid",
         "valid_at": "2025-01-01", "invalid_at": "2026-01-01"},
        {"text": "My name is Carlos Delgado and I play chess on weekends.",
         "created_at": "2026-02-10T12:00:00"},
    ]
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    m = _mk()
    out = import_zep(m, path, user_id="carlos")
    assert out["triples"] == 3
    assert out["texts"] == 1
    view = m.search("Where does Carlos work now?", user_id="carlos")
    ctx = view["context_block"].lower()
    assert "vercel" in ctx
    view = m.search("What is Carlos's hobby?", user_id="carlos")
    assert "chess" in view["context_block"].lower()
    m.close()


# ------------------------------------------------------------------ chroma
def test_migrate_chroma_embeddings_schema(tmp_path):
    """Chroma's sqlite3 store: embeddings table with a document column."""
    db = str(tmp_path / "chroma.sqlite3")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE embeddings (id TEXT PRIMARY KEY, "
                 "document TEXT, embedding BLOB)")
    for i, doc in enumerate([
            "Dana works at Anthropic as a researcher.",
            "Dana lives in Austin.",
            "Dana prefers green tea."]):
        conn.execute("INSERT INTO embeddings(id, document, embedding) "
                     "VALUES(?,?,?)", (f"emb-{i}", doc, b"\x00" * 8))
    conn.commit()
    conn.close()

    m = _mk()
    out = import_chroma(m, db, user_id="dana")
    assert out["documents"] == 3
    assert out["facts"] >= 3
    view = m.search("Where does Dana work?", user_id="dana")
    assert "anthropic" in view["context_block"].lower()
    m.close()


def test_migrators_registry_covers_plan_targets():
    assert set(MIGRATORS) == {"mem0", "zep", "chroma"}


def test_cli_migrate_end_to_end(tmp_path):
    """The literal command from the plan: cortexm migrate --from mem0."""
    from context_m.cli import main as cli_main
    db = str(tmp_path / "mem0.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, data TEXT, "
                 "created_at TEXT, updated_at TEXT)")
    conn.execute("INSERT INTO history(data, created_at, updated_at) "
                 "VALUES(?,?,?)",
                 (json.dumps({"messages": [
                     {"role": "user",
                      "content": "Elena works at Figma as a designer."}]}),
                  "2026-01-01", "2026-01-01"))
    conn.commit()
    conn.close()
    target = str(tmp_path / "target.db")
    rc = cli_main(["migrate", "--from", "mem0", "--path", db,
                   "--db", target, "--user-id", "elena"])
    assert rc == 0
    m = Memory(Config(db_path=target))
    view = m.search("Where does Elena work?", user_id="elena")
    assert "figma" in view["context_block"].lower()
    m.close()
