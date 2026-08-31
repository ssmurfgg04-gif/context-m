"""Measure honest comparison-table numbers: context tokens + latency.

- facts-section tokens: the [Memory — Known facts] block the reader
  emits for a top-10 search (the 'memory' an agent consumes)
- full context_block tokens: facts + verbatim evidence (what the
  benchmark judge consumes)
- search latency p50/p95 over a warmed corpus
"""
import os
import re
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.longmemeval_canonical_full import (
    _make_config, _flatten_haystack_rich)
from cortexm.api.memory import Memory

DATA = "benchmarks/data/longmemeval/longmemeval_s_cleaned.json"
DB = "/tmp/cortexm_tokens_test/tok.db"


def _tokens(text):
    # GPT-style approximation: 4 chars/token
    return len(text) // 4


def main():
    import json
    for suf in ("", "-wal", "-shm", "-journal"):
        try:
            os.unlink(DB + suf)
        except FileNotFoundError:
            pass
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    data = json.load(open(DATA))
    # one real multi-session question with a big haystack
    q = next(e for e in data if e['question_id'].startswith('d851d5ba'))
    cfg = _make_config(DB)
    mem = Memory(cfg)
    msgs = _flatten_haystack_rich(q['haystack_sessions'],
                                  haystack_dates=q.get('haystack_dates', []))
    t0 = time.time()
    for i in range(0, len(msgs), 50):
        mem.add(msgs[i:i + 50], user_id="u1")
    t_ingest = time.time() - t0

    query = q['question']
    out = mem.search(query, user_id="u1", limit=10)
    cb = out.get("context_block", "")
    # split sections
    facts_part = cb.split("## VERBATIM", 1)[0]
    n_facts = _tokens(facts_part)
    n_full = _tokens(cb)

    # latency: 30 searches (mix of queries) on the warmed corpus
    queries = [query, "What is my dog's name?", "Where do I live?",
               "How much did I raise for charity?", "charity events",
               "cycling training tips", "my family", "work", "travel",
               "food preferences"] * 3
    times = []
    for qq in queries:
        t0 = time.perf_counter()
        mem.search(qq, user_id="u1", limit=10)
        times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    p50 = times[len(times) // 2]
    p95 = times[int(len(times) * 0.95)]
    print(f"ingest: {len(msgs)} msgs in {t_ingest:.1f}s")
    print(f"facts-section tokens (top-10 facts): ~{n_facts}")
    print(f"full context_block tokens (with verbatim evidence): ~{n_full}")
    print(f"search p50: {p50:.2f} ms | p95: {p95:.2f} ms (n={len(times)})")
    mem.close()
    for suf in ("", "-wal", "-shm", "-journal"):
        try:
            os.unlink(DB + suf)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
