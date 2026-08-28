"""Tests for the 4 new modules from the research-steals round 2:
nightly-consolidate CLI, sidecar blob arena migration, engineered
role vectors, SPARQL endpoint.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from cortexm.api.memory import Memory
from cortexm.config import Config
from cortexm.vsa.role_vectors import EngineeredRoleVectors
from cortexm.bridge.decoders import RDFDecoder, get_decoder
from cortexm.server.sparql import (
    parse_sparql, match_triple, apply_filters,
    execute_sparql, SparqlServer)


# ----------------------------------------------------------- fixtures
@pytest.fixture
def mem():
    cfg = Config.from_env()
    cfg.db_path = tempfile.mktemp(suffix=".db")
    m = Memory(cfg)
    yield m
    m.close()
    if os.path.exists(cfg.db_path):
        os.unlink(cfg.db_path)


# ----------------------------------------------------------- (a) CLI + nightly
class TestConsolidateCLI:
    """Verify the cortexm consolidate CLI runs both passes and
    respects the --dry-run / --no-lifecycle / --no-dreaming flags."""

    def test_consolidate_runs_both_passes(self, mem):
        mem.add([{"role": "user",
                   "content": "My name is Alice. I work at Google."}],
                user_id="alice")
        mem.add([{"role": "user",
                   "content": "Alice works at Google."}], user_id="alice")
        out = mem.consolidate()
        assert "lifecycle" in out
        assert "dreaming" in out
        # dreaming pass should report its keys
        assert "merged_pairs" in out["dreaming"]
        assert "retired_facts" in out["dreaming"]
        assert "palace_defragged" in out["dreaming"]

    def test_consolidate_dry_run(self, mem):
        mem.add([{"role": "user", "content": "I work at Google."}],
                user_id="alice")
        out = mem.consolidate(dry_run=True)
        assert out["dreaming"]["dry_run"] is True
        assert out["dreaming"]["commit_id"] is None

    def test_consolidate_skip_lifecycle(self, mem):
        out = mem.consolidate(lifecycle=False)
        assert out["lifecycle"] == {}
        assert "merged_pairs" in out["dreaming"]

    def test_consolidate_skip_dreaming(self, mem):
        out = mem.consolidate(dreaming=False)
        assert out["dreaming"] == {}
        assert "promoted" in out["lifecycle"]


# ----------------------------------------------------------- (b) blob arena
class TestBlobArena:
    def test_enable_blob_arena_migrates_chunks(self, mem):
        # ingest long-form chunks
        long_text = "Conversation transcript: " + ("hello " * 200)
        mem.add([{"role": "user", "content": long_text}], user_id="u1")
        mem.add([{"role": "user", "content": long_text}], user_id="u2")
        # before migration: chunks.text is the full text
        rows = mem.store.conn.execute("SELECT text FROM chunks").fetchall()
        assert all(len(r[0]) > 100 for r in rows)
        # run migration
        arena_path = tempfile.mktemp(suffix=".blb")
        report = mem.enable_blob_arena(arena_path)
        assert report["migrated"] == 2
        assert report["skipped"] == 0
        # after migration: chunks.text is the 64-byte preview
        rows = mem.store.conn.execute(
            "SELECT text, blob_offset, blob_len FROM chunks").fetchall()
        for preview, offset, length in rows:
            assert len(preview) <= 67  # 64 + "..."
            assert offset is not None
            assert length > 0
        # full text recoverable via arena
        first_chunk_id = mem.store.conn.execute(
            "SELECT id FROM chunks LIMIT 1").fetchone()[0]
        full_text = mem.get_chunk_text(first_chunk_id)
        assert "Conversation transcript" in full_text
        assert len(full_text) > 100
        # cleanup
        if os.path.exists(arena_path):
            os.unlink(arena_path)

    def test_blob_arena_idempotent(self, mem):
        mem.add([{"role": "user", "content": "x" * 500}], user_id="u1")
        arena_path = tempfile.mktemp(suffix=".blb")
        # first migration: migrates the chunk
        r1 = mem.enable_blob_arena(arena_path)
        assert r1["migrated"] == 1
        # second migration: nothing to migrate (already has blob_offset)
        r2 = mem.enable_blob_arena(arena_path)
        assert r2["migrated"] == 0
        if os.path.exists(arena_path):
            os.unlink(arena_path)


# ----------------------------------------------------------- (c) role vectors
class TestEngineeredRoleVectors:
    def test_ae_fit_recovers_principal_directions(self):
        # build a fact matrix where the top principal direction is
        # obvious — first dim is much larger than the others
        import numpy as np
        n = 200
        d = 64
        rng = np.random.default_rng(42)
        # first dim dominates
        X = rng.standard_normal((n, d)).astype(np.float32) * 0.1
        X[:, 0] = rng.standard_normal(n).astype(np.float32) * 10.0
        X[:, 1] = rng.standard_normal(n).astype(np.float32) * 5.0
        X[:, 2] = rng.standard_normal(n).astype(np.float32) * 2.0
        erv = EngineeredRoleVectors(dims=d, n_roles=3, n_epochs=100, lr=0.01)
        report = erv.fit(X)
        assert report["trained"] is True
        assert erv.is_fit
        # the top role vector should be mostly aligned with dim 0
        # (the dominant axis)
        s_vec = erv.role_vec("S")
        assert s_vec is not None
        # top-1 principal direction should have a large component
        # on the axis with the highest variance
        top_axis = int(np.argmax(np.abs(s_vec)))
        # may not be exactly 0 but should be one of {0, 1, 2}
        # since those are the high-variance dims
        assert top_axis in (0, 1, 2)

    def test_role_vectors_orthogonal_after_fit(self):
        import numpy as np
        # uncorrelated data — top-3 principal directions should
        # be approximately orthogonal by construction
        n, d = 500, 128
        rng = np.random.default_rng(7)
        X = rng.standard_normal((n, d)).astype(np.float32)
        erv = EngineeredRoleVectors(dims=d, n_roles=3, n_epochs=150)
        erv.fit(X)
        v1 = erv.role_vec("S")
        v2 = erv.role_vec("R")
        v3 = erv.role_vec("V")
        # off-diagonal of W @ W.T should be small
        W = np.stack([v1, v2, v3])
        gram = W @ W.T
        off_diag = float(np.sum(np.abs(gram - np.eye(3))))
        assert off_diag < 0.1, f"off_diag={off_diag} — vectors not orthogonal"

    def test_save_load_roundtrip(self, tmp_path):
        import numpy as np
        n, d = 50, 32
        rng = np.random.default_rng(11)
        X = rng.standard_normal((n, d)).astype(np.float32)
        erv = EngineeredRoleVectors(dims=d, n_roles=3, n_epochs=50)
        erv.fit(X)
        path = tmp_path / "erv.npz"
        erv.save(str(path))
        erv2 = EngineeredRoleVectors(dims=d, n_roles=3)
        erv2.load(str(path))
        assert erv2.is_fit
        assert np.allclose(erv2.role_vec("S"), erv.role_vec("S"))

    def test_vsa_uses_engineered_when_set(self):
        """VSA.role_vec should return the engineered vector when
        EngineeredRoleVectors is attached."""
        import numpy as np
        from cortexm.vsa.ops import VSA
        vsa = VSA(dims=32, mode="conv")
        erv = EngineeredRoleVectors(dims=32, n_roles=3, n_epochs=30)
        erv.fit(np.random.default_rng(0).standard_normal((100, 32))
                 .astype(np.float32))
        vsa.use_engineered(erv)
        # VSA should now serve the engineered vector
        v_s = vsa.role_vec("S")
        assert v_s is not None
        assert np.allclose(v_s, erv.role_vec("S"))

    def test_memory_use_engineered_role_vectors(self, mem):
        mem.add([{"role": "user",
                   "content": "My name is Alice. I work at Google. "
                              "I live in SF. I have a cat named Bob."}],
                user_id="alice")
        report = mem.use_engineered_role_vectors(n_epochs=50)
        assert report.get("trained", False) is True
        assert mem.palace.vsa._engineered is not None
        assert mem.palace.vsa._engineered.is_fit


# ----------------------------------------------------------- (d) SPARQL
class TestSparql:
    def test_parse_simple_select(self):
        q = "SELECT ?s ?p ?o WHERE { ?s ?p ?o }"
        parsed = parse_sparql(q)
        assert parsed["select"] == ["?s", "?p", "?o"]
        assert parsed["patterns"] == [("?s", "?p", "?o")]
        assert parsed["filters"] == []

    def test_parse_with_filter_regex(self):
        q = ('SELECT ?s ?o WHERE { ?s ?p ?o . '
             'FILTER regex(?o, "Google", "i") }')
        parsed = parse_sparql(q)
        assert len(parsed["patterns"]) == 1
        assert len(parsed["filters"]) == 1
        assert parsed["filters"][0]["type"] == "regex"
        assert parsed["filters"][0]["var"] == "?o"
        assert parsed["filters"][0]["pattern"] == "Google"
        assert parsed["filters"][0]["flags"] == "i"

    def test_parse_with_filter_equals(self):
        q = 'SELECT ?s ?o WHERE { ?s ?p ?o . FILTER(?p = "name") }'
        parsed = parse_sparql(q)
        assert parsed["filters"][0]["type"] == "equals"
        assert parsed["filters"][0]["value"] == "name"

    def test_match_triple_with_vars(self):
        bindings = match_triple(("alice", "name", "Alice Smith"),
                                  ("?s", "?p", "?o"))
        assert bindings == {"?s": "alice", "?p": "name",
                            "?o": "Alice Smith"}

    def test_match_triple_with_literal_fails_on_mismatch(self):
        # pattern says p must be the literal "name" — fact has "name"
        bindings = match_triple(("alice", "name", "Alice"),
                                  ("?s", "name", "?o"))
        assert bindings is not None
        # but if fact has different relation, no match
        bindings = match_triple(("alice", "age", "30"),
                                  ("?s", "name", "?o"))
        assert bindings is None

    def test_apply_filters_regex(self):
        b = {"?o": "Google Inc"}
        # case-insensitive match
        ok = apply_filters(b, [{"type": "regex", "var": "?o",
                                  "pattern": "google", "flags": "i"}])
        assert ok is True
        # case-sensitive fails
        ok = apply_filters(b, [{"type": "regex", "var": "?o",
                                  "pattern": "google", "flags": ""}])
        assert ok is False

    def test_execute_sparql_returns_results(self):
        facts = [
            ("alice", "name", "Alice"),
            ("alice", "works_at", "Google"),
            ("bob", "name", "Bob"),
            ("bob", "works_at", "Microsoft"),
        ]
        # SELECT all triples
        r = execute_sparql("SELECT ?s ?p ?o WHERE { ?s ?p ?o }", facts)
        assert r["n_results"] == 4
        # FILTER by relation
        r = execute_sparql(
            'SELECT ?s ?o WHERE { ?s ?p ?o . FILTER(?p = "works_at") }',
            facts)
        assert r["n_results"] == 2

    def test_sparql_server_end_to_end(self, mem):
        """Boot the SPARQL server in a thread, query it, verify
        the result."""
        import threading
        import urllib.request
        import urllib.parse
        import time
        mem.add([{"role": "user", "content": "My name is Alice. "
                                            "I work at Google."}],
                user_id="alice")
        mem.add([{"role": "user", "content": "My name is Bob. "
                                            "I work at Microsoft."}],
                user_id="bob")
        # all facts (alice and bob share a user_id namespace here
        # since we used 'alice' and 'bob' as separate user_ids)
        server = SparqlServer(mem, host="127.0.0.1", port=8917)
        server.start()
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            time.sleep(0.3)
            q = 'SELECT ?s ?p ?o WHERE { ?s ?p ?o }'
            url = f"http://127.0.0.1:8917/?{urllib.parse.urlencode({'query': q})}"
            with urllib.request.urlopen(url, timeout=2) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            assert result["n_results"] > 0
            # FILTER test — find all facts where value contains "Google"
            q = ('SELECT ?s ?o WHERE { ?s ?p ?o . '
                 'FILTER regex(?o, "Google", "i") }')
            url = f"http://127.0.0.1:8917/?{urllib.parse.urlencode({'query': q})}"
            with urllib.request.urlopen(url, timeout=2) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            assert result["n_results"] >= 1
            assert any("Google" in row.get("?o", "")
                        for row in result["results"]["bindings"])
        finally:
            server.stop()


# ----------------------------------------------------------- combined
class TestBenchScriptsRun:
    """Smoke tests — verify the bench scripts at least parse + run
    end-to-end on a tiny corpus."""

    def test_blob_arena_bench_runs(self):
        # run the script as a subprocess with a tiny corpus
        import subprocess
        result = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "migrate_blob_arena.py"),
             "--size", "5", "--long-text-len", "500"],
            capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, f"stderr: {result.stderr[-500:]}"
        assert "Graph-size reduction" in result.stdout

    def test_role_vectors_bench_runs(self):
        import subprocess
        result = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "train_role_vectors.py"),
             "--size", "10", "--epochs", "30"],
            capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, f"stderr: {result.stderr[-500:]}"
        assert "cross-talk" in result.stdout

    def test_sparql_demo_runs(self):
        import subprocess
        result = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "sparql_demo.py")],
            capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, f"stderr: {result.stderr[-500:]}"
        assert "HONEST SUMMARY" in result.stdout
