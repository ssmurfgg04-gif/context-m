"""Tests for cortexm.experimental (v0.6.4 RuVector borrows).

graph_recall: entity adjacency index + 2-hop walks
coherence:    temporal coherence reranking

All μ=0: same inputs → same outputs, no LLM, no learned weights.
"""
from __future__ import annotations

import gc
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from cortexm.api.memory import Memory
from cortexm.config import Config
from cortexm.experimental.graph_recall import (
    EntityGraphIndex, graph_recall_boost, _entity_tokens)
from cortexm.experimental.coherence import (
    coherence_scores, _parse_valid_from)


# ------------------------------------------------------------------ graph
class TestEntityTokens:
    def test_subject_and_value_tokens(self):
        toks = _entity_tokens("user:bob", "dog named Charlie")
        assert "user:bob" in toks
        assert "bob" in toks
        assert "dog" in toks
        assert "charlie" in toks
        assert "named" not in toks  # stopword

    def test_empty_is_safe(self):
        assert _entity_tokens("", "") == []


class TestEntityGraphIndex:
    def _mem(self):
        cfg = Config(db_path=":memory:")
        m = Memory(config=cfg)
        m.add([{"role": "user", "content":
                "I just got a job at Meta. My dog's name is Charlie."}],
               user_id="bob")
        m.add([{"role": "user", "content":
                "My sister Anna lives in Berlin."}], user_id="bob")
        return m

    def test_walk_finds_anchor_facts(self):
        m = self._mem()
        idx = EntityGraphIndex(m.store)
        scores, stats = idx.walk("Where does Anna live", "bob")
        facts = {f.id: f for f in m.store.query_facts(user_id="bob")}
        hit = [facts[fid] for fid in scores
               if fid in facts and facts[fid].subject == "Anna"]
        assert hit, f"Anna fact not walked: {scores}, {stats}"
        assert stats["n_query_tokens"] >= 1
        m.close()

    def test_walk_respects_scope(self):
        m = self._mem()
        all_ids = {f.id for f in m.store.query_facts(user_id="bob")}
        # empty scope → nothing returned
        scores, _ = idx = EntityGraphIndex(m.store).walk(
            "Anna Charlie Meta", "bob", scope=frozenset())
        assert not scores
        # full scope → results
        scores2, _ = EntityGraphIndex(m.store).walk(
            "Anna Charlie Meta", "bob", scope=all_ids)
        assert scores2
        m.close()

    def test_walk_is_deterministic(self):
        m = self._mem()
        idx = EntityGraphIndex(m.store)
        s1, _ = idx.walk("Where does Anna live", "bob")
        s2, _ = idx.walk("Where does Anna live", "bob")
        assert s1 == s2
        m.close()

    def test_index_rebuilds_on_fact_change(self):
        m = self._mem()
        idx = EntityGraphIndex(m.store)
        before, _ = idx.walk("Anna", "bob")
        assert before
        # phrasing the pet_named pattern actually extracts
        m.add([{"role": "user", "content":
                "My cat's name is Whiskers."}], user_id="bob")
        after, stats = idx.walk("Whiskers", "bob")
        assert after, "index did not rebuild after new fact"
        m.close()

    def test_facade_dedupes_store_instances(self):
        m = self._mem()
        b1, _ = graph_recall_boost(m.store, "Anna", "bob")
        b2, _ = graph_recall_boost(m.store, "Anna", "bob")
        assert b1 == b2
        m.close()


class TestGraphRecallWiring:
    def test_search_surfaces_connected_fact(self):
        cfg = Config(db_path=":memory:", graph_recall_enabled=True)
        m = Memory(config=cfg)
        m.add([{"role": "user", "content":
                "My sister Anna lives in Berlin and works at Zalando."}],
               user_id="u1")
        m.add([{"role": "user", "content":
                "My brother Paul lives in Munich."}], user_id="u1")
        out = m.search("Where does Anna live?", user_id="u1")
        blob = str(out)
        assert "Berlin" in blob
        m.close()

    def test_disabled_flag_turns_it_off(self):
        cfg = Config(db_path=":memory:", graph_recall_enabled=False)
        m = Memory(config=cfg)
        m.add([{"role": "user", "content":
                "My sister Anna lives in Berlin."}], user_id="u1")
        # smoke: search still works with the module off
        out = m.search("Where does Anna live?", user_id="u1")
        assert out is not None
        m.close()

    def test_search_is_deterministic_across_calls(self):
        cfg = Config(db_path=":memory:")
        m = Memory(config=cfg)
        m.add([{"role": "user", "content":
                "I got a job at Meta. My dog's name is Charlie. "
                "Charlie is a beagle."}], user_id="u1")
        # warm-up call first: the query-time lazy re-extraction path may
        # materialize new facts on the FIRST call (by design, v0.5.7);
        # determinism is asserted between consecutive steady-state calls.
        m.reader.search("What is my dog's name?", user_id="u1")
        r1 = m.reader.search("What is my dog's name?", user_id="u1")
        r2 = m.reader.search("What is my dog's name?", user_id="u1")
        ids1 = [f.id for f in r1.facts]
        ids2 = [f.id for f in r2.facts]
        assert ids1 == ids2
        m.close()


# ------------------------------------------------------------------ coherence
class TestParseValidFrom:
    def test_date_only(self):
        dt = _parse_valid_from("2026-08-31")
        assert dt == datetime(2026, 8, 31)

    def test_datetime_forms(self):
        assert _parse_valid_from("2026-08-31T10:30:00") is not None
        assert _parse_valid_from("2026-08-31 10:30:00") is not None

    def test_garbage_returns_none(self):
        assert _parse_valid_from(None) is None
        assert _parse_valid_from("") is None
        assert _parse_valid_from("not-a-date") is None


class _F:
    """Minimal fact stand-in for coherence scoring."""

    def __init__(self, fid, subject, value, valid_from):
        self.id = fid
        self.subject = subject
        self.value = value
        self.valid_from = valid_from


class TestCoherenceScores:
    def test_clustered_facts_score_higher_than_isolated(self):
        d0 = "2026-06-01"
        facts = [
            _F("a", "user:bob", "job at Meta", d0),
            _F("b", "user:bob", "moved to Menlo Park", d0),
            _F("c", "Meta", "office in Menlo Park", d0),
            _F("iso", "user:bob", "likes poetry", "2026-01-01"),
        ]
        coh = coherence_scores(facts, window_days=30)
        assert coh["a"] > coh["iso"]
        assert coh["iso"] == 0.0

    def test_far_apart_facts_do_not_corroborate(self):
        # 60 days apart > 30-day window → no shared boost
        facts = [
            _F("a", "user:bob", "job at Meta", "2026-01-01"),
            _F("b", "user:bob", "job at Google", "2026-03-01"),
        ]
        coh = coherence_scores(facts, window_days=30)
        assert coh["a"] == 0.0 and coh["b"] == 0.0

    def test_no_shared_entity_no_corroboration(self):
        facts = [
            _F("a", "user:bob", "job at Meta", "2026-01-01"),
            _F("b", "Anna", "lives in Berlin", "2026-01-02"),
        ]
        coh = coherence_scores(facts, window_days=30)
        assert coh["a"] == 0.0 and coh["b"] == 0.0

    def test_empty_and_single(self):
        assert coherence_scores([]) == {}
        coh = coherence_scores([_F("a", "s", "v", "2026-01-01")])
        assert coh == {"a": 0.0}

    def test_scores_bounded_zero_one(self):
        facts = [_F(f"f{i}", f"e{i % 3}", f"v{i % 3}", "2026-01-0" + str(1 + i % 5))
                 for i in range(10)]
        coh = coherence_scores(facts, window_days=30)
        assert all(0.0 <= v <= 1.0 for v in coh.values())


class TestCoherenceWiring:
    def test_reader_search_with_coherence_on(self):
        cfg = Config(db_path=":memory:", coherence_weight_enabled=True)
        m = Memory(config=cfg)
        m.add([{"role": "user", "content":
                "I started a new job at Meta in June. "
                "The team is great."}], user_id="u1")
        m.add([{"role": "user", "content":
                "In June I also adopted a beagle named Charlie."}],
               user_id="u1")
        res = m.reader.search("What did I do in June?", user_id="u1")
        assert res.facts  # retrieval still returns results
        m.close()

    def test_config_flags_exist_with_defaults(self):
        cfg = Config(db_path=":memory:")
        assert cfg.graph_recall_enabled is True
        assert cfg.graph_recall_weight > 0
        assert cfg.coherence_weight_enabled is True
        assert cfg.coherence_window_days == 30
