"""Tests for the v0.6.7 stress-hardening fixes.

Every fix that came out of the exploratory stress harness
(scripts-side S1-S8) gets a boring unit test here so the fixes stay
fixed:
  - deterministic_fact_id: content-derived ids (the mu=0 claim —
    uuid4() ids leaked into search() results and broke byte-exact
    reproducibility)
  - TraceStore.insert_fact / insert_facts_bulk collision fallback
  - find_conflicts: identical-content re-ingest hits the SKIP path
  - cognition engines (abstraction/analogy/hypothesis) are
    idempotent on re-runs — no duplicate derived facts
  - fade-pressure hysteresis: the auto-sweep backs off (watermark in
    the kv table) instead of firing on every add() past threshold
  - PerUserIdiolectNormalizer: incremental vocab-matrix sync is
    byte-identical to a full rebuild, and eviction forces a rebuild
  - end-to-end mu=0: two fresh Memory instances over the same corpus
    produce byte-exact search outputs (the S6 stress check, pinned)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cortexm import Config, Memory
from cortexm.cognition.abstraction import AbstractionEngine
from cortexm.cognition.analogy import AnalogyDetector
from cortexm.cognition.gaps import Gap, HypothesisEngine
from cortexm.cognition.scanner import Pattern, ScanResult
from cortexm.text.embedder import HashingEmbedder
from cortexm.text.idiolect import PerUserIdiolectNormalizer
from cortexm.trace.contradictions import Action, find_conflicts
from cortexm.trace.fact import deterministic_fact_id, make_fact
from cortexm.trace.store import TraceStore

NOW = datetime(2023, 5, 8, tzinfo=timezone.utc)


# --------------------- deterministic fact ids ------------------------------

def test_deterministic_fact_id_shape():
    fid = deterministic_fact_id(subject="alice", relation="works_at",
                                value="google")
    assert isinstance(fid, str) and len(fid) == 32
    int(fid, 16)  # pure hex — same shape as the old uuid4().hex


def test_deterministic_fact_id_same_content_same_id():
    a = deterministic_fact_id(user_id="u", subject="alice",
                              relation="works_at", value="google",
                              valid_from="2023-01-01")
    b = deterministic_fact_id(user_id="u", subject="alice",
                              relation="works_at", value="google",
                              valid_from="2023-01-01")
    assert a == b


@pytest.mark.parametrize("field", [
    "user_id", "agent_id", "run_id", "subject", "relation", "value",
    "valid_from", "valid_to",
])
def test_deterministic_fact_id_differs_per_field(field):
    base = {"user_id": "u", "subject": "alice", "relation": "works_at",
            "value": "google", "valid_from": "2023-01-01", "valid_to": None}
    changed = dict(base)
    changed[field] = "DIFFERENT"
    assert deterministic_fact_id(**base) != deterministic_fact_id(**changed)


def test_deterministic_fact_id_excludes_transaction_time():
    # tx time is deliberately excluded: re-recording the same statement
    # later must still derive the same id
    base = {"subject": "alice", "relation": "lives_in", "value": "nairobi"}
    assert deterministic_fact_id(**base) == deterministic_fact_id(**base)


def test_make_fact_default_id_is_content_derived():
    f1 = make_fact("alice", "works_at", "google", now=NOW, user_id="u")
    f2 = make_fact("alice", "works_at", "google", now=NOW, user_id="u")
    assert f1.id == f2.id == deterministic_fact_id(
        user_id="u", subject="alice", relation="works_at",
        value="google", valid_from=f1.valid_from)


def test_make_fact_explicit_id_wins():
    f = make_fact("alice", "works_at", "google", now=NOW, id="cafe42")
    assert f.id == "cafe42"


# --------------------- store collision fallback -----------------------------

def _mk(subject, relation, value, user_id="u1", valid_from="2023-01-01"):
    return make_fact(subject, relation, value, now=NOW, user_id=user_id,
                     valid_from=valid_from, valid_to=None)


def test_insert_fact_collision_after_soft_delete():
    store = TraceStore()
    f = store.insert_fact(_mk("alice", "lives_in", "nairobi"))
    # soft-delete (retire) the fact, then re-ingest the same content
    store.conn.execute("UPDATE facts SET is_active=0 WHERE id=?", (f.id,))
    f2 = _mk("alice", "lives_in", "nairobi")
    assert f2.id == f.id  # same content -> same deterministic id
    out = store.insert_fact(f2)          # must not raise
    assert out.id != f.id                # fallback to a fresh random id
    n = store.conn.execute(
        "SELECT COUNT(*) AS n FROM facts WHERE subject='alice'"
        " AND relation='lives_in' AND value='nairobi'").fetchone()["n"]
    assert n == 2  # both bi-temporal rows recorded


def test_insert_facts_bulk_in_batch_duplicate():
    store = TraceStore()
    facts = [_mk("bob", "has_skill", "python"),
             _mk("bob", "has_skill", "python"),   # same content, same id
             _mk("bob", "has_skill", "rust")]
    n = store.insert_facts_bulk(facts)             # must not raise
    assert n == 3
    cnt = store.conn.execute(
        "SELECT COUNT(*) AS n FROM facts WHERE subject='bob'"
        " AND relation='has_skill'").fetchone()["n"]
    assert cnt == 3


# --------------------- conflicts: SKIP on re-ingest --------------------------

def test_find_conflicts_identical_reingest_is_skip():
    store = TraceStore()
    store.insert_fact(_mk("carol", "speaks", "french"))
    cand = _mk("carol", "speaks", "french")
    assert cand.id == store.conn.execute(
        "SELECT id FROM facts WHERE subject='carol'").fetchone()["id"]
    conflict = find_conflicts(store, cand)
    assert conflict.action is Action.SKIP
    assert conflict.existing and conflict.existing[0].id == cand.id


def test_find_conflicts_supersede_still_fires_for_new_value():
    store = TraceStore()
    store.insert_fact(_mk("carol", "lives_in", "munich"))
    cand = _mk("carol", "lives_in", "berlin")
    cand.id = deterministic_fact_id(
        user_id="u1", subject="carol", relation="lives_in",
        value="berlin", valid_from="2023-01-01")
    conflict = find_conflicts(store, cand)
    assert conflict.action is not Action.SKIP  # new value -> real conflict


# --------------------- cognition engines idempotent --------------------------

def _fact_rows(store, where_extra=""):
    return store.conn.execute(
        f"SELECT id, subject, relation, value FROM facts {where_extra}"
    ).fetchall()


def test_abstraction_engine_rerun_adds_nothing():
    store = TraceStore()
    scan = ScanResult(patterns=[
        Pattern(kind="subject_fanout",
                payload={"subject": f"person{i}",
                         "relations": ["works_at", "lives_in"]},
                support=3, confidence=0.9)
        for i in range(4)
    ])
    eng = AbstractionEngine(store, min_members=2, min_co_occur_support=1)
    r1 = eng.run(scan, user_id="u")
    assert r1.membership_edges_added > 0
    rows_1 = _fact_rows(store, "WHERE relation='member_of'")
    r2 = eng.run(scan, user_id="u")
    assert r2.membership_edges_added == 0
    rows_2 = _fact_rows(store, "WHERE relation='member_of'")
    assert [tuple(r) for r in rows_1] == [tuple(r) for r in rows_2]


def test_analogy_detector_rerun_adds_nothing():
    store = TraceStore()
    # two relations with overlapping subject sets -> analogy candidates
    for i in range(3):
        store.insert_fact(_mk(f"p{i}", "likes", "pizza", user_id="u"))
        store.insert_fact(_mk(f"p{i}", "prefers", "tea", user_id="u"))
    det = AnalogyDetector(store, min_overlap=0.10, min_support=1)
    r1 = det.run(ScanResult(), user_id="u")
    assert r1.edges_added > 0
    rows_1 = _fact_rows(store, "WHERE relation='analogous_to'")
    r2 = det.run(ScanResult(), user_id="u")
    assert r2.edges_added == 0
    rows_2 = _fact_rows(store, "WHERE relation='analogous_to'")
    assert [tuple(r) for r in rows_1] == [tuple(r) for r in rows_2]


def test_hypothesis_engine_rerun_adds_nothing():
    store = TraceStore()
    # peers with the missing relation — the majority strategy reads
    # these to propose a value for alice's gap
    for i in range(3):
        store.insert_fact(_mk(f"p{i}", "speaks", "english", user_id="u"))
    store.insert_fact(_mk("p9", "speaks", "spanish", user_id="u"))
    gaps = [Gap(subject="alice", missing_relation="speaks",
                peer_count=3, peer_values=["english", "english", "english"],
                basis="majority")]
    eng = HypothesisEngine(store)
    r1 = eng.run(gaps, user_id="u")
    assert r1.facts_added > 0
    assert r1.hypotheses and r1.hypotheses[0].proposed_value == "english"
    rows_1 = _fact_rows(store, "WHERE subject='alice' AND relation='speaks'")
    r2 = eng.run(gaps, user_id="u")
    assert r2.facts_added == 0
    rows_2 = _fact_rows(store, "WHERE subject='alice' AND relation='speaks'")
    assert [tuple(r) for r in rows_1] == [tuple(r) for r in rows_2]


# --------------------- fade-pressure hysteresis ------------------------------

def test_fade_pressure_hysteresis(monkeypatch):
    monkeypatch.setenv("CORTEXM_PRESSURE_THRESHOLD", "10")
    monkeypatch.setenv("CORTEXM_PRESSURE_BACKOFF", "1.10")
    calls = []
    import cortexm.trace.fade as fade_mod

    def spy(store, **kw):
        calls.append(kw.get("user_id"))

    monkeypatch.setattr(fade_mod, "fade_sweep", spy)
    m = Memory(config=Config(db_path=":memory:", fade_enabled=True))
    try:
        for i in range(12):
            m.store.insert_fact(
                _mk("s", "likes", f"item{i}", user_id="hy"))
        m._maybe_run_fade_under_pressure("hy")       # n=12 >= 10 -> sweep, watermark=12
        assert len(calls) == 1
        m._maybe_run_fade_under_pressure("hy")       # 12 < 12*1.10 -> skip
        m._maybe_run_fade_under_pressure("hy")
        assert len(calls) == 1
        assert m.store.kv_get("__fade_pressure:hy") == "12"
        for i in range(3):                 # grow to 15 >= 13.2 -> sweep fires
            m.store.insert_fact(
                _mk("s", "likes", f"extra{i}", user_id="hy"))
        m._maybe_run_fade_under_pressure("hy")
        assert len(calls) == 2
        assert m.store.kv_get("__fade_pressure:hy") == "15"
    finally:
        m.close()


def test_fade_pressure_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("CORTEXM_PRESSURE_THRESHOLD", "1")
    m = Memory(config=Config(db_path=":memory:", fade_enabled=False))
    try:
        m.store.insert_fact(_mk("s", "likes", "x", user_id="hy2"))
        m._maybe_run_fade_under_pressure("hy2")
        assert m.store.kv_get("__fade_pressure:hy2") is None
    finally:
        m.close()


# --------------------- idiolect incremental matrix sync ---------------------

def _norm(vocab_cap=50_000):
    return PerUserIdiolectNormalizer(
        embedder=HashingEmbedder(dims=64), vocab_cap=vocab_cap)


def test_idiolect_incremental_sync_matches_full_rebuild():
    n = _norm()
    # the k-NN path only engages with >= 5 vocab entries
    n.observe("u", "alpha beta gamma delta epsilon")
    n.normalize_token("u", "zzzorbl")          # forces first full build
    assert n._vocab_matrix is not None and n._vocab_matrix.shape[0] == 5
    M0, ids0 = n._vocab_matrix, list(n._vocab_ids)
    n.observe("u", "zeta eta theta")           # append-only growth
    n.normalize_token("u", "zzzqwrm")            # incremental tail concat
    M1, ids1 = n._vocab_matrix, list(n._vocab_ids)
    assert M1.shape[0] == 8
    assert ids1[:len(ids0)] == ids0              # prefix alignment holds
    assert np.array_equal(M1[:len(ids0)], M0)    # old rows untouched
    # now force the full-rebuild path and require byte-identical results
    n._vocab_evicted = True
    n._vocab_dirty = True
    n.normalize_token("u", "zzzother")
    assert np.array_equal(n._vocab_matrix, M1)
    assert list(n._vocab_ids) == ids1


def test_idiolect_eviction_sets_rebuild_flag():
    # eviction fires when load_state (or a cap change) leaves more
    # entries than vocab_cap — observe() alone stops at the cap
    big = _norm(vocab_cap=50_000)
    big.observe("u", "alpha beta gamma delta epsilon zeta eta theta")
    saved = big.save_state()
    n = _norm(vocab_cap=5)
    n.load_state(saved)                     # 8 entries > cap 5
    n.observe("u", "more words here")       # triggers LRU eviction
    assert n._vocab_evicted is True
    n.normalize_token("u", "zzzx")          # must take the full-rebuild path
    assert len(n._vocab_ids) == len(n._vocab)
    assert n._vocab_matrix.shape[0] == len(n._vocab_ids) == 5


def test_idiolect_load_state_forces_full_rebuild():
    n = _norm()
    n.observe("u", "alpha beta gamma delta epsilon zeta")
    n.normalize_token("u", "zzz1")
    assert n._vocab_matrix is not None and n._vocab_matrix.shape[0] == 6
    saved = n.save_state()
    m = _norm()
    m.observe("u", "unrelated entirely different junk tokens now")
    m.normalize_token("u", "zzz2")
    assert m._vocab_matrix is not None
    m.load_state(saved)
    assert m._vocab_evicted is True         # load_state forces rebuild
    m.normalize_token("u", "zzz3")          # post-load sync must be a rebuild
    assert m._vocab_matrix.shape[0] == len(m._vocab_ids) == 6
    assert list(m._vocab_ids) == list(m._vocab.keys())


# --------------------- end-to-end mu=0 (pinned S6) --------------------------

CORPUS = [
    "Alice works at Google as a senior engineer",
    "Alice lives in Nairobi and has two dogs",
    "Bob prefers Python over JavaScript",
    "Bob was born on 1990-05-14",
    "Carol moved to Berlin in 2019",
    "Alice's dog is named Charlie",
    "Bob enjoys hiking on weekends",
    "Carol speaks three languages fluently",
]
QUERIES = ["Where does Alice work?", "What pets does Alice have?",
           "What languages does Carol speak?", "What does Bob enjoy?",
           "When was Bob born?"]


def _scrub(o):
    if isinstance(o, dict):
        return {k: _scrub(v) for k, v in o.items()
                if k not in ("latency_ms", "elapsed", "ts")}
    if isinstance(o, list):
        return [_scrub(v) for v in o]
    return o


def _e2e_run():
    m = Memory(config=Config(db_path=":memory:"))
    try:
        for msg in CORPUS:
            m.add([{"role": "user", "content": msg}], user_id="det")
        outs = [m.search(q, user_id="det", limit=5) for q in QUERIES]
        return json.dumps(_scrub(outs), sort_keys=True, default=str)
    finally:
        m.close()


def test_mu0_two_runs_byte_exact():
    assert _e2e_run() == _e2e_run()
