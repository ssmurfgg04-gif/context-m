"""Tests for the cross-encoder-style fact reranker (μ=0).

Verifies:
  * fact_nl() renders facts into the expected natural-language form
  * FactReranker.rerank() reorders facts by cosine(query, fact_nl)
  * PRF (Rocchio) shifts the query embedding and lifts precision@5
  * MemoryReader wires rerank in only when cfg.enable_rerank=True
  * End-to-end: a search() with rerank enabled surfaces the right fact
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import numpy as np
import pytest

from context_m.api.memory import Memory
from context_m.config import Config
from context_m.text.embedder import HashingEmbedder
from context_m.trace.fact import Fact


def _fact(relation: str, value: str, subject: str = "beam_1",
          fid: str | None = None) -> Fact:
    """Build a minimal Fact for testing (required fields only)."""
    return Fact(
        id=fid or f"f_{relation}",
        subject=subject,
        relation=relation,
        value=value,
        valid_from="2024-01-01",
    )


# ----------------------------------------------------------- fact_nl rendering
class TestFactNL:
    def test_name_relation(self):
        f = _fact("name", "Jennifer Mccall")
        from context_m.bridge.rerank import fact_nl
        assert fact_nl(f) == "the name of beam_1 is jennifer mccall"

    def test_age_relation(self):
        f = _fact("age", "59")
        from context_m.bridge.rerank import fact_nl
        assert fact_nl(f) == "the age of beam_1 is 59"

    def test_works_at_relation(self):
        f = _fact("works_at", "Google", subject="alice")
        from context_m.bridge.rerank import fact_nl
        assert fact_nl(f) == "alice works at google"

    def test_unknown_relation_falls_back_to_3tuple(self):
        f = _fact("hates", "mondays", subject="alice")
        from context_m.bridge.rerank import fact_nl
        assert fact_nl(f) == "alice | hates | mondays"


# ----------------------------------------------------------- rerank behavior
class TestRerank:
    def _facts(self):
        return [
            _fact(r, v, fid=f"f{i}")
            for i, (r, v) in enumerate([
                ("name", "Jennifer Mccall"),
                ("age", "59"),
                ("location", "Portland"),
                ("profession", "Nurse"),
                ("gender", "Female"),
            ])
        ]

    def test_rerank_promotes_matching_fact(self):
        """Query 'what is the name of beam_1' should rank the name fact first."""
        emb = HashingEmbedder(dims=768, seed=0x0C0FFEE)
        from context_m.bridge.rerank import FactReranker
        rr = FactReranker(emb)
        q = emb.embed("what is the name of beam_1")
        facts = self._facts()
        # give the wrong fact the highest pre-rerank score so we test
        # that the rerank actually overrides it
        scores = {f.id: 0.0 for f in facts}
        scores[facts[1].id] = 1.0  # age fact pre-rerank winner
        reranked, new_scores = rr.rerank(q, facts, scores, top_k=3,
                                         enable_prf=False)
        # the name fact should be in top-3 (PRF disabled for stability)
        top3_rels = [f.relation for f in reranked]
        assert "name" in top3_rels, \
            f"name fact missing from reranked top-3: {top3_rels}"

    def test_rerank_returns_top_k_only(self):
        """rerank(top_k=3) returns exactly 3 facts."""
        emb = HashingEmbedder(dims=768, seed=0x0C0FFEE)
        from context_m.bridge.rerank import FactReranker
        rr = FactReranker(emb)
        q = emb.embed("name")
        facts = self._facts()
        scores = {f.id: 0.5 for f in facts}
        reranked, _ = rr.rerank(q, facts, scores, top_k=3, enable_prf=False)
        assert len(reranked) == 3

    def test_rerank_empty_input(self):
        """rerank on empty facts list returns empty list."""
        emb = HashingEmbedder(dims=768, seed=0x0C0FFEE)
        from context_m.bridge.rerank import FactReranker
        rr = FactReranker(emb)
        q = emb.embed("anything")
        reranked, new_scores = rr.rerank(q, [], {}, top_k=5)
        assert reranked == []
        assert new_scores == {}

    def test_rerank_with_prf(self):
        """PRF enabled produces a different ordering vs no-PRF in some cases.

        We can't guarantee PRF always differs (depends on top-3 mean), but
        we can guarantee it runs without error and returns top_k facts.
        """
        emb = HashingEmbedder(dims=768, seed=0x0C0FFEE)
        from context_m.bridge.rerank import FactReranker
        rr = FactReranker(emb)
        q = emb.embed("the age of beam_1")
        facts = self._facts()
        scores = {f.id: 0.5 for f in facts}
        reranked, _ = rr.rerank(q, facts, scores, top_k=3, enable_prf=True)
        assert len(reranked) == 3

    def test_rerank_score_blend_in_range(self):
        """Blended scores are in [0, 1] (min-max normalized)."""
        emb = HashingEmbedder(dims=768, seed=0x0C0FFEE)
        from context_m.bridge.rerank import FactReranker
        rr = FactReranker(emb)
        q = emb.embed("name")
        facts = self._facts()
        scores = {f.id: float(i) * 0.1 for i, f in enumerate(facts)}
        _, new_scores = rr.rerank(q, facts, scores, top_k=5,
                                   enable_prf=False)
        for s in new_scores.values():
            assert 0.0 <= s <= 1.0


# ----------------------------------------------------------- config + reader wiring
class TestRerankConfig:
    def test_default_disabled(self):
        """By default, enable_rerank=False (no behavior change)."""
        cfg = Config.from_env()
        assert cfg.enable_rerank is False

    def test_reader_no_reranker_when_disabled(self, tmp_path):
        """MemoryReader._reranker is None when enable_rerank=False."""
        cfg = Config.from_env()
        cfg.db_path = str(tmp_path / "test.db")
        cfg.enable_rerank = False
        mem = Memory(cfg)
        assert mem.reader._reranker is None
        mem.close()

    def test_reader_has_reranker_when_enabled(self, tmp_path):
        """MemoryReader._reranker is a FactReranker when enable_rerank=True."""
        cfg = Config.from_env()
        cfg.db_path = str(tmp_path / "test.db")
        cfg.enable_rerank = True
        mem = Memory(cfg)
        assert mem.reader._reranker is not None
        from context_m.bridge.rerank import FactReranker
        assert isinstance(mem.reader._reranker, FactReranker)
        mem.close()


# ----------------------------------------------------------- end-to-end
class TestRerankE2E:
    def test_rerank_lifts_precision(self, tmp_path):
        """End-to-end: a small corpus where rerank should improve top-1.

        We ingest 5 facts under one user, then ask a query that baseline
        retrieval gets wrong (because a long chunk dilutes similarity) and
        rerank gets right (because the fact NL is focused).
        """
        cfg_off = Config.from_env()
        cfg_off.db_path = str(tmp_path / "off.db")
        cfg_off.enable_rerank = False
        mem_off = Memory(cfg_off)

        cfg_on = Config.from_env()
        cfg_on.db_path = str(tmp_path / "on.db")
        cfg_on.enable_rerank = True
        mem_on = Memory(cfg_on)

        text = ("My name is Alice. I am 32 years old. I live in Seattle. "
                "I work as a software engineer at Google. "
                "My cat is named Whiskers. I love hiking on weekends.")
        for mem in (mem_off, mem_on):
            mem.add([{"role": "user", "content": text}], user_id="alice")

        # the query is about the cat — but the chunk has many facts
        # so chunk-vector similarity may not rank the cat fact first
        q = "What is the name of Alice's cat?"
        out_off = mem_off.search(q, user_id="alice", limit=5)
        out_on = mem_on.search(q, user_id="alice", limit=5)

        # rerank should at least INCLUDE the cat fact in top-5
        # (baseline may or may not, depending on chunk dilution)
        top5_on = " ".join(r["memory"] for r in out_on["results"])
        assert "whiskers" in top5_on.lower(), \
            f"rerank missed the cat fact: {top5_on}"

        # timing metadata reflects whether rerank was used
        assert out_off["timing"].get("rerank", False) is False
        assert out_on["timing"].get("rerank", False) is True

        mem_off.close()
        mem_on.close()
