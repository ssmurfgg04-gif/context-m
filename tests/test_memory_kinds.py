"""Memory kinds tests: fact vs experience classification, kind-filtered
retrieval, invalid-kind rejection, and schema migration on old databases.

Port of Hindsight's world-facts-vs-experiences split, deterministic:
no LLM, heuristic documented in cortexm.trace.fact.classify_kind.
"""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cortexm.api.memory import Memory
from cortexm.config import Config
from cortexm.trace.fact import classify_kind
from cortexm.trace.store import TraceStore


def _mem(d):
    return Memory(Config(db_path=os.path.join(d, "t.db")))


def _close(m):
    try:
        m.close()
    except Exception:
        pass


class TestClassifyKind:
    def test_lived_event_is_experience(self):
        assert classify_kind("I fixed the login bug yesterday") == "experience"
        assert classify_kind("I went to Kisumu last week") == "experience"
        assert classify_kind("We shipped the release on Friday") == "experience"

    def test_world_fact_stays_fact(self):
        assert classify_kind("Paris is the capital of France") == "fact"
        assert classify_kind("My name is Bob") == "fact"
        assert classify_kind("I like strong coffee") == "fact"

    def test_assistant_speech_is_fact(self):
        assert classify_kind("I fixed the bug", speaker="assistant") == "fact"

    def test_empty_is_fact(self):
        assert classify_kind("") == "fact"


class TestKindEndToEnd:
    def test_experience_stored_with_kind(self):
        with tempfile.TemporaryDirectory() as d:
            m = _mem(d)
            m.add([{"role": "user",
                    "content": "I visited the Nairobi office in June and met Wanjiku."}],
                  user_id="kinds")
            rows = m.store.conn.execute(
                "SELECT subject, relation, kind FROM facts WHERE user_id='kinds'"
            ).fetchall()
            assert rows, "expected extracted facts"
            kinds = {r[2] for r in rows}
            assert "experience" in kinds, f"kinds seen: {kinds}"
            _close(m)

    def test_search_kind_filter(self):
        with tempfile.TemporaryDirectory() as d:
            m = _mem(d)
            m.add([{"role": "user",
                    "content": "I fixed the login bug yesterday morning."}],
                  user_id="kinds")
            m.add([{"role": "user",
                    "content": "The login service runs on port 8080."}],
                  user_id="kinds")
            all_res = m.search("login", user_id="kinds")
            assert len(all_res.get("results", [])) >= 1
            exp = m.search("login", user_id="kinds", kind="experience")
            for f in exp.get("results", []):
                assert f.kind == "experience", f
            _close(m)

    def test_invalid_kind_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            m = _mem(d)
            with pytest.raises(ValueError, match="unknown kind"):
                m.search("anything", user_id="kinds", kind="dream")
            _close(m)


class TestKindMigration:
    def test_old_db_gains_kind_column(self):
        # Simulate a pre-kinds database: full schema, then remove the
        # kind column the way an old release's schema looked.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            seed = TraceStore(db_path=path)
            seed.conn.execute(
                "INSERT INTO facts (id, subject, relation, value, "
                "valid_from, tx_from, user_id) "
                "VALUES ('f1','Bob','works_at','Acme','2024-01-01',"
                "'2024-01-01','migration')")
            seed.conn.commit()
            seed.conn.execute("DROP INDEX IF EXISTS idx_facts_kind")
            seed.conn.execute("ALTER TABLE facts DROP COLUMN kind")
            seed.conn.commit()
            seed.close()
            probe = sqlite3.connect(path)
            try:
                cols_before = {r[1] for r in
                               probe.execute("PRAGMA table_info(facts)").fetchall()}
            finally:
                probe.close()
            assert "kind" not in cols_before
            # Reopening through TraceStore must add the column with default.
            store = TraceStore(db_path=path)
            cols = {r[1] for r in
                    store.conn.execute("PRAGMA table_info(facts)").fetchall()}
            assert "kind" in cols
            val = store.conn.execute(
                "SELECT kind FROM facts WHERE id='f1'").fetchone()[0]
            assert val == "fact"
            store.close()
            store.conn.close()
