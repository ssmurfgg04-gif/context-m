"""Tests for the research-steal modules: typed edges, decoders,
consolidate, blob arena, chaos mode."""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from cortexm.config import Config
from cortexm.api.memory import Memory
from cortexm.trace.edges import (
    CAUSAL, REFERS_TO, ALL_KINDS, is_causal, is_truth_maintenance,
    is_semantic_ref, direction_convention,
    wire_causal_edge, wire_refers_to, find_causal_chain,
)
from cortexm.trace.consolidate import consolidate, consolidation_report
from cortexm.trace.blob_arena import (
    BlobArena, migrate_chunks_to_arena, get_chunk_text,
)
from cortexm.bridge.decoders import (
    LLMPromptDecoder, RDFDecoder, DatalogDecoder, JSONDecoder,
    get_decoder, DECODERS,
)


# ---------- Typed edges (Aeon steal) ------------------------------------

class TestTypedEdges:
    def test_vocab_has_causal_and_refers_to(self):
        assert CAUSAL in ALL_KINDS
        assert REFERS_TO in ALL_KINDS

    def test_categorization(self):
        assert is_causal(CAUSAL)
        assert is_truth_maintenance("CONTRADICTS")
        assert is_semantic_ref(REFERS_TO)

    def test_direction_convention(self):
        assert direction_convention(CAUSAL) == "cause_to_effect"
        assert direction_convention("CONTRADICTS") == "new_to_old"
        assert direction_convention(REFERS_TO) == "ref_to_target"

    def test_wire_causal_edge(self):
        from cortexm.config import Config
        mem = Memory(Config())
        # add two facts, wire causal between them
        from cortexm.trace.fact import make_fact
        from datetime import datetime, timezone
        f1 = make_fact("alice", "works_at", "google", now=datetime.now(timezone.utc),
                       user_id="u1", source_id="", source_hash="")
        f2 = make_fact("alice", "works_at", "stripe", now=datetime.now(timezone.utc),
                       user_id="u1", source_id="", source_hash="")
        mem.store.insert_fact(f1, "c1")
        mem.store.insert_fact(f2, "c1")
        wire_causal_edge(mem.store, f2.id, f1.id, reason="left google for stripe")
        edges = mem.store.edges_of(f2.id, CAUSAL, "out")
        assert len(edges) == 1
        assert edges[0]["dst"] == f1.id

    def test_find_causal_chain_ancestors(self):
        from cortexm.config import Config
        mem = Memory(Config())
        from cortexm.trace.fact import make_fact
        from datetime import datetime, timezone
        t = datetime.now(timezone.utc)
        fs = []
        for i, v in enumerate(["a", "b", "c"]):
            f = make_fact("x", "p", v, now=t, user_id="u1",
                          source_id="", source_hash="")
            mem.store.insert_fact(f, "c1")
            fs.append(f)
        # wire: f2 -> f1, f3 -> f2 (so f3 is the cause, f1 is the
        # final effect; the chain is f3 → f2 → f1)
        wire_causal_edge(mem.store, fs[1].id, fs[0].id, "step1")
        wire_causal_edge(mem.store, fs[2].id, fs[1].id, "step2")
        # 'descendants' from f3 should walk src->dst forwards and
        # return [f2, f1] (the chain of effects f3 caused)
        chain = find_causal_chain(mem.store, fs[2].id, direction="descendants")
        assert fs[1].id in chain, f"expected f2 in chain, got {chain}"
        assert fs[0].id in chain, f"expected f1 in chain, got {chain}"
        # 'ancestors' from f1 should walk backwards and return [f2, f3]
        # (the chain of causes that led to f1)
        ancestors = find_causal_chain(mem.store, fs[0].id, direction="ancestors")
        assert fs[1].id in ancestors, f"expected f2 in ancestors, got {ancestors}"
        assert fs[2].id in ancestors, f"expected f3 in ancestors, got {ancestors}"


# ---------- Swappable decoders (NSR steal) ------------------------------

class TestDecoders:
    def test_registry(self):
        assert set(DECODERS) == {"llm_prompt", "rdf", "datalog", "json"}

    def test_get_decoder_unknown(self):
        with pytest.raises(ValueError):
            get_decoder("nonsense")

    def test_rdf_decoder(self):
        from cortexm.trace.fact import make_fact
        from datetime import datetime, timezone
        f = make_fact("alice", "works_at", "Google", now=datetime.now(timezone.utc),
                      user_id="u1", source_id="", source_hash="")
        d = RDFDecoder()
        out = d.render(query="q", intent="recall", facts=[f],
                       scores={}, notes=[], store=None)
        assert "alice" in out
        assert "works_at" in out
        assert "Google" in out
        assert out.endswith(" .")

    def test_datalog_decoder(self):
        from cortexm.trace.fact import make_fact
        from datetime import datetime, timezone
        f = make_fact("alice", "works_at", "Google", now=datetime.now(timezone.utc),
                      user_id="u1", source_id="", source_hash="")
        d = DatalogDecoder()
        out = d.render(query="q", intent="recall", facts=[f],
                       scores={}, notes=[], store=None)
        # Datalog atoms are lowercase; bare atom (no quotes) for
        # alnum-only values, quoted otherwise
        assert "works_at(alice, google)." in out or 'works_at(alice, "Google").' in out, \
            f"got {out!r}"

    def test_json_decoder(self):
        from cortexm.trace.fact import make_fact
        from datetime import datetime, timezone
        f = make_fact("alice", "works_at", "Google", now=datetime.now(timezone.utc),
                      user_id="u1", source_id="", source_hash="")
        d = JSONDecoder()
        out = d.render(query="q", intent="recall", facts=[f],
                       scores={f.id: 0.9}, notes=None, store=None)
        import json
        parsed = json.loads(out)
        assert parsed[0]["subject"] == "alice"
        assert parsed[0]["relation"] == "works_at"
        assert parsed[0]["value"] == "Google"
        assert parsed[0]["score"] == 0.9

    def test_reader_with_decoder(self):
        # v0.5.3: disable verbatim tier for this test so the
        # context_block ends exactly as the decoder rendered it
        # (verbatim would append "## VERBATIM CHUNKS" section).
        cfg = Config()
        cfg.verbatim_search_enabled = False
        cfg.verbatim_ingest_enabled = False
        cfg.recall_step_in_search = False
        mem = Memory(cfg)
        mem.add([{"role": "user", "content": "My name is Alice and I work at Google."}],
                user_id="u1")
        # swap decoder
        mem.reader.with_decoder("rdf")
        r = mem.search("where does Alice work?", user_id="u1", limit=5)
        # the context_block is now RDF-formatted
        assert "works_at" in r["context_block"]
        assert r["context_block"].endswith(" .")
        # swap back
        mem.reader.with_decoder("llm_prompt")
        r2 = mem.search("where does Alice work?", user_id="u1", limit=5)
        assert "[Memory" in r2["context_block"]


# ---------- Consolidate / dreaming (Aeon steal) ------------------------

class TestConsolidate:
    def test_dry_run_doesnt_commit(self):
        mem = Memory(Config())
        mem.add([{"role": "user", "content": "I work at Google as a swe."}],
                user_id="u1")
        before = len(mem.store.query_facts(active=True))
        stats = consolidate(mem.store, mem.palace, dry_run=True)
        after = len(mem.store.query_facts(active=True))
        assert stats["dry_run"] is True
        assert stats["commit_id"] is None
        assert before == after

    def test_merges_near_duplicate_facts(self):
        mem = Memory(Config())
        # insert two IDENTICAL facts directly via the store — bypass
        # the writer's lifecycle dedupe so consolidate has something
        # to merge. (In normal operation the lifecycle dedupes exact
        # restatements; consolidate's job is to catch near-dupes the
        # lifecycle misses, e.g. 'Alice works at Google' vs
        # 'Alice is employed at Google' which both produce slightly
        # different fact values.)
        from cortexm.trace.fact import make_fact
        from datetime import datetime, timezone
        t = datetime.now(timezone.utc)
        for _ in range(2):
            f = make_fact('alice', 'works_at', 'Google', now=t,
                          user_id='u1', source_id='', source_hash='')
            mem.store.insert_fact(f, 'c1')
        before = len(mem.store.query_facts(user_id='u1', active=True))
        stats = consolidate(mem.store, mem.palace,
                            merge_threshold=0.5, dry_run=False)
        after = len(mem.store.query_facts(user_id='u1', active=True))
        # at least one pair should be merged
        assert stats["merged_pairs"] >= 1, f"expected merge, got {stats}"
        assert after < before, f"expected fewer after, {before} -> {after}"

    def test_report(self):
        mem = Memory(Config())
        from cortexm.trace.fact import make_fact
        from datetime import datetime, timezone
        t = datetime.now(timezone.utc)
        for _ in range(2):
            f = make_fact('alice', 'works_at', 'Google', now=t,
                          user_id='u1', source_id='', source_hash='')
            mem.store.insert_fact(f, 'c1')
        consolidate(mem.store, mem.palace, merge_threshold=0.5, dry_run=False)
        rep = consolidation_report(mem.store)
        assert rep["merged_facts"] >= 1, f"got {rep}"


# ---------- Sidecar blob arena (Aeon steal) ----------------------------

class TestBlobArena:
    def test_put_and_get(self):
        with tempfile.TemporaryDirectory() as td:
            arena = BlobArena(os.path.join(td, "blobs.dat"))
            blob_id, offset, length, compressed = arena.put(
                "hello world this is a test", compress=False)
            text = arena.get_text(offset, length, compressed)
            assert text == "hello world this is a test"
            arena.close()

    def test_compression(self):
        with tempfile.TemporaryDirectory() as td:
            arena = BlobArena(os.path.join(td, "blobs.dat"))
            long_text = "the quick brown fox " * 100
            _, offset, length, compressed = arena.put(long_text, compress=True)
            assert compressed is True
            text = arena.get_text(offset, length, compressed)
            assert text == long_text
            arena.close()

    def test_migrate_chunks_to_arena(self):
        with tempfile.TemporaryDirectory() as td:
            arena_path = os.path.join(td, "blobs.dat")
            arena = BlobArena(arena_path)
            mem = Memory(Config())
            long_text = "the quick brown fox " * 50 + "and done."
            mem.add([{"role": "user", "content": long_text}],
                    user_id="u1")
            # migrate
            stats = migrate_chunks_to_arena(mem.store, arena)
            assert stats["migrated"] >= 1
            # the chunk's text column now has a 64-byte preview
            from cortexm.trace.store import TraceStore
            row = mem.store.conn.execute(
                "SELECT text, blob_offset, blob_len FROM chunks "
                "WHERE user_id=? LIMIT 1", ("u1",)).fetchone()
            text_preview, offset, length = row
            assert len(text_preview) <= 67  # 64 + "..."
            assert offset is not None
            # full text via arena
            full = get_chunk_text(mem.store, arena, row[0] if False else
                                  mem.store.conn.execute(
                                      "SELECT id FROM chunks WHERE user_id=? LIMIT 1",
                                      ("u1",)).fetchone()[0])
            assert full.startswith("the quick brown fox")
            assert full.endswith("and done.")
            arena.close()


# ---------- Chaos mode (EAM steal) ------------------------------------

class TestChaosMode:
    def test_chaos_ingest(self):
        from cortexm.api.chaos import chaos_ingest
        mem = Memory(Config())
        n = chaos_ingest(mem, [
            "yo im alice n i work @ google rn as a swe",
            "tbh i live in austin n my bday is march 5",
            "ngl my sister Priya works at Stripe",
        ], user_id="u1")
        # at least SOME facts landed in the trace
        facts = mem.store.query_facts(user_id="u1", active=True)
        assert len(facts) >= 1
        assert n["stats"]["facts_inserted"] >= 1, f"got {n}"
