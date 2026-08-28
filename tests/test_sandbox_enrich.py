"""Tests for the scope sandbox (InjecMEM isolation) and enrichment fallback."""

import datetime as dt

import pytest

from cortexm import Memory
from cortexm.config import Config

TS = dt.datetime(2026, 1, 10, tzinfo=dt.timezone.utc)


def _mk(**cfg_over):
    # tiny_fallback (deterministic 2-layer self-attention catch-all) is on
    # by default in production. For these enrichment tests we disable it
    # so the LLM extractor has chunks to find — the test is asserting the
    # enrichment path works, not that the deterministic fallback preempts
    # it. (In production, tiny_fallback fires before enrich() is even
    # eligible to find anything; that's a feature, not a bug.)
    cfg_over.setdefault("tiny_fallback_enabled", False)
    cfg = Config(**cfg_over)
    return Memory(cfg)


# ------------------------------------------------------------ sandbox
def test_agent_facts_invisible_to_user_scope():
    m = _mk()
    m.add("My name is Alice Johnson. I work at Google.",
          user_id="alice", timestamp=TS)
    m.add("Alice lives in Toronto.", user_id="alice",
          agent_id="agent-x", timestamp=TS)

    user_view = m.search("Where does Alice live?", user_id="alice")
    assert "toronto" not in user_view["context_block"].lower(), (
        "agent-scoped fact leaked into user-scope view")

    agent_view = m.search("Where does Alice live?", user_id="alice",
                          agent_id="agent-x")
    assert "toronto" in agent_view["context_block"].lower(), (
        "agent must see its own scope")


def test_promotion_makes_fact_visible():
    m = _mk()
    m.add("Alice lives in Toronto.", user_id="alice",
          agent_id="agent-x", timestamp=TS)
    fid = [f["id"] for f in m.get_all(user_id="alice", agent_id="agent-x")["results"]
           if "toronto" in f["memory"].lower()][0]

    out = m.promote([fid], reviewed_by="admin")
    assert fid in out["promoted"]

    user_view = m.search("Where does Alice live?", user_id="alice")
    assert "toronto" in user_view["context_block"].lower()


def test_promotion_refused_for_low_confidence():
    m = _mk(sandbox_promote_min_confidence=0.9)
    m.add("Alice lives in Toronto.", user_id="alice",
          agent_id="agent-x", timestamp=TS)
    fid = [f["id"] for f in m.get_all(user_id="alice", agent_id="agent-x")["results"]
           if "toronto" in f["memory"].lower()][0]
    out = m.promote([fid])
    assert fid in [r["id"] for r in out["refused"]]
    assert m.search("Where does Alice live?", user_id="alice")[
        "context_block"].lower().find("toronto") == -1


def test_sandbox_disabled_restores_old_behaviour():
    m = _mk(sandbox_enabled=False)
    m.add("Alice lives in Toronto.", user_id="alice",
          agent_id="agent-x", timestamp=TS)
    user_view = m.search("Where does Alice live?", user_id="alice")
    assert "toronto" in user_view["context_block"].lower()


def test_user_facts_visible_to_agents():
    m = _mk()
    m.add("My name is Alice Johnson. I work at Google.",
          user_id="alice", timestamp=TS)
    agent_view = m.search("Where does Alice work?", user_id="alice",
                          agent_id="agent-y")
    assert "google" in agent_view["context_block"].lower(), (
        "agents must still see shared user facts")


# ------------------------------------------------------------ enrichment
def test_enrichment_fallback_with_fake_llm():
    m = _mk()
    # indirect phrasing the patterns miss
    m.add("Did I mention I finally left Stripe? Anyway these days "
          "it's Netflix life for me.",
          user_id="bob", timestamp=TS)
    before = m.search("Where does Bob work now?", user_id="bob")

    def fake_llm(texts, subjects):
        return [[
            {"subject": "Bob", "relation": "left", "value": "Stripe",
             "confidence": 0.9},
            {"subject": "Bob", "relation": "works_at", "value": "Netflix",
             "confidence": 0.95},
        ] for _ in texts]

    rep = m.enrich("bob", extractor=fake_llm)
    assert rep["facts_committed"] >= 2
    after = m.search("Where does Bob work now?", user_id="bob")
    assert "netflix" in after["context_block"].lower()
    # provenance marks the enrichment path — auditable
    fact_rows = m.store.query_facts(user_id="bob", active=None)
    assert any(f.provenance.get("pattern") == "llm_enrichment"
               for f in fact_rows), "enriched facts must carry llm_enrichment provenance"
    # μ=0 honesty: the counter records the LLM usage
    assert m.stats()["counters"]["llm_calls"] >= 1


def test_enrichment_dry_run_makes_no_changes():
    m = _mk()
    m.add("ya i quit google btw, doing the anthropic thing now lol",
          user_id="carol", timestamp=TS)
    rep = m.enrich("carol", dry_run=True)
    assert rep["llm_calls"] == 0
    assert rep["facts_committed"] == 0


def test_enriched_facts_confidence_capped():
    m = _mk()
    m.add("Je travaille chez Vercel a present.", user_id="dan",
          timestamp=TS)

    def arrogant_llm(texts, subjects):
        return [[{"subject": "Dan", "relation": "works_at",
                  "value": "Vercel", "confidence": 1.0}] for _ in texts]

    m.enrich("dan", extractor=arrogant_llm)
    for f in m.get_all(user_id="dan")["results"]:
        if "vercel" in f["memory"].lower():
            assert f.get("confidence", 1.0) <= 0.85 + 1e-9, (
                "enriched facts must stay confidence-capped below "
                "deterministic facts")
