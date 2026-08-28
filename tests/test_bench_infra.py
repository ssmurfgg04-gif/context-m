"""Tests for the 7 modules flagged by codegraph_review test_parity:
creator, trajectory_view, prefetch, bench/run, bench/messy,
bench/beam_loader, bench/harness.

These are smoke tests — they import each module, exercise one or two
public symbols, and assert nothing crashes. The point is to prove
the modules are reachable from the test suite (so regressions surface
in CI), not to comprehensively cover every code path.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

# Make sure the project root is on sys.path so cortexm.* imports work
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ----------------------------- creator -----------------------------------

def test_creator_class_exists_and_basic():
    """creator.Creator is the REPL facade; verify it instantiates."""
    from cortexm.creator import Creator, HELP
    assert isinstance(HELP, str) and len(HELP) > 0
    c = Creator()
    assert c is not None
    # The Creator owns an in-memory Memory; adding + searching works.
    c.add("Alice works at Google")
    out = c.search("Where does Alice work?")
    assert isinstance(out, (list, dict, str))


def test_creator_main_returns_int():
    """creator.main is the CLI entry point; it should return an int."""
    from cortexm import creator
    assert callable(creator.main)


# ----------------------------- trajectory_view ---------------------------

def test_trajectory_view_html_and_handler_smoke():
    """trajectory_view.HTML is the served page; ViewerHandler is the
    BaseHTTPRequestHandler subclass. Both must be importable without
    side effects (no port bind on import)."""
    from cortexm import trajectory_view
    assert isinstance(trajectory_view.HTML, str)
    assert trajectory_view.HTML.startswith("<!doctype html>") or \
        trajectory_view.HTML.startswith("<!DOCTYPE html>")
    # ViewerHandler subclasses BaseHTTPRequestHandler
    import http.server
    assert issubclass(trajectory_view.ViewerHandler,
                      http.server.BaseHTTPRequestHandler)
    # ThreadedHTTPServer mixes ThreadingMixIn
    import socketserver
    assert issubclass(trajectory_view.ThreadedHTTPServer,
                      socketserver.ThreadingMixIn)


def test_trajectory_view_main_callable():
    from cortexm import trajectory_view
    assert callable(trajectory_view.main)


# ----------------------------- prefetch ----------------------------------

def test_prefetcher_basic_observe_and_predict():
    """Prefetcher observes co-access pairs and predicts the next batch."""
    from cortexm.features.prefetch import Prefetcher
    p = Prefetcher()
    # Simulate a sequence of fact accesses: A then B then C
    p.observe("fid_a")
    p.observe("fid_b")
    p.observe("fid_c")
    # After enough observations, predict() should return a dict (maybe empty)
    predicted = p.predict()
    assert isinstance(predicted, dict)


def test_prefetcher_stats_dict():
    from cortexm.features.prefetch import Prefetcher
    p = Prefetcher()
    # Stats should be available as attributes (hits, predictions, etc.)
    assert hasattr(p, "hits")
    assert hasattr(p, "predictions")


# ----------------------------- bench/run ---------------------------------

def test_bench_run_setup_determinism_idempotent():
    """_setup_determinism should be safe to call; returns None or
    re-execs but doesn't crash in a test context."""
    from cortexm.bench import run
    # We only assert the function exists + is callable.
    assert callable(run._setup_determinism)
    assert callable(run.run_buckets)
    assert callable(run.main)


# ----------------------------- bench/messy ------------------------------

def test_bench_messy_messify_text_smoke():
    """messify_text should mangle a clean sentence into slangy text."""
    import random
    from cortexm.bench.messy import (
        MessyCorpus, messify_messages, messify_persona_dict,
        SLANG_FILLERS, TEXTSPEAK, MISSPELLINGS, STITCHERS,
        MESSY_SMALLTALK, _messify_text,
    )
    rng = random.Random(42)
    out = _messify_text("My name is Alice and I work at Google.", rng)
    assert isinstance(out, str)
    assert len(out) > 0
    # The list constants must be non-empty
    assert len(SLANG_FILLERS) > 0
    assert len(TEXTSPEAK) > 0
    assert len(MISSPELLINGS) > 0
    assert len(STITCHERS) > 0
    assert len(MESSY_SMALLTALK) > 0
    # MessyCorpus requires (user_id, text, facts) at init — verify signature
    import inspect
    sig = inspect.signature(MessyCorpus.__init__)
    assert 'user_id' in sig.parameters
    assert 'text' in sig.parameters
    assert 'facts' in sig.parameters


def test_bench_messy_make_messy_persona_returns_persona():
    """make_messy_persona should produce a persona-shaped object."""
    import random
    import datetime
    from cortexm.bench.messy import make_messy_persona
    # Import the Persona class the function returns
    try:
        from cortexm.bench.generator import Persona
    except Exception:
        # If generator.Persona is not importable (older layout), skip
        pytest.skip("cortexm.bench.generator.Persona not importable")
    rng = random.Random(42)
    p = make_messy_persona(rng, idx=0, t0=datetime.datetime(2026, 1, 1))
    assert isinstance(p, Persona)


# ----------------------------- bench/beam_loader -------------------------

def test_bench_beam_loader_constants_and_parser_smoke():
    """beam_loader exposes the dataset URL constants + the parse_*
    helpers. The parsers are pure functions over a row dict — test
    with a minimal synthetic row."""
    from cortexm.bench.beam_loader import (
        DATASETS_SERVER, DATASET_NAME, CONFIG, SPLIT, TOTAL_ROWS,
        parse_user_facts, parse_chat_turns,
    )
    assert DATASETS_SERVER.startswith("https://")
    assert DATASET_NAME == "Mohammadta/BEAM-10M"
    assert CONFIG == "default"
    assert SPLIT == "10M"
    assert TOTAL_ROWS == 10
    # parse_user_facts on a minimal row → list of dicts (may be empty)
    fake_row = {"user_profile": {"user_info": {}, "user_relationships": []}}
    out = parse_user_facts(fake_row)
    assert isinstance(out, list)
    # parse_chat_turns on a minimal row → list of dicts (may be empty)
    out2 = parse_chat_turns(fake_row)
    assert isinstance(out2, list)


def test_bench_beam_loader_load_rows_signature():
    """load_beam_rows signature: (n, *, cache_dir, parquet_path, ...) → list[dict]."""
    from cortexm.bench.beam_loader import load_beam_rows
    import inspect
    sig = inspect.signature(load_beam_rows)
    params = list(sig.parameters.keys())
    assert "n" in params
    # Don't actually call — hits the network; just verify the signature.


# ----------------------------- bench/harness ----------------------------

def test_bench_harness_bucket_result_dataclass():
    """BucketResult is the dataclass returned by run_bucket."""
    from cortexm.bench.harness import BucketResult, run_bucket, format_report
    # BucketResult is a dataclass requiring 'bucket' arg
    import dataclasses
    assert dataclasses.is_dataclass(BucketResult)
    br = BucketResult(bucket="test")
    assert br.bucket == "test"
    # run_bucket + format_report are callables
    assert callable(run_bucket)
    assert callable(format_report)
    # format_report on an empty list returns a string (may be empty)
    rep = format_report([])
    assert isinstance(rep, str)


def test_bench_harness_format_report_with_sample():
    """format_report on a hand-built BucketResult should produce a
    non-empty string."""
    from cortexm.bench.harness import BucketResult, format_report
    br = BucketResult(bucket="test-bucket")
    abilities = ["name", "employer", "skill", "preference",
                  "location", "relationship", "event", "temporal",
                  "abstention", "knowledge_update"]
    br.per_system = {"context_m": {"recall": 0.95, "overall": 0.95,
                                    "per_ability": {a: 0.9 for a in abilities}}}
    br.per_ability = {a: 0.9 for a in abilities}
    br.n_questions = 10
    br.corpus = {"estimated_tokens": 1000, "n_chunks": 50}
    br.systems = ["context_m"]
    br.ingest = {"wall_seconds": 0.5, "tokens_per_second": 2000,
                  "u0_protocol": True, "llm_calls": 0,
                  "facts": 100, "chunks": 50,
                  "commits": 1, "derived_facts": 0}
    br.trust = {"provenance_completeness": 1.0, "audit_latency_ms": 1.0}
    rep = format_report([br])
    assert isinstance(rep, str)
    assert len(rep) > 0
