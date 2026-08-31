"""Smoke test: graph recall + coherence wiring end-to-end."""
import sys
sys.path.insert(0, "/home/z/my-project")

from cortexm.api.memory import Memory
from cortexm.config import Config

# multi-session scenario: job at Meta in session 1, dog Charlie + move
# in session 2, lookalike noise in other sessions
cfg = Config(db_path=":memory:",
             graph_recall_enabled=True,
             coherence_weight_enabled=True)
m = Memory(config=cfg)
uid = "alice"

m.add([{"role": "user", "content": "I just got a job at Meta in Menlo Park."}],
      user_id=uid)
m.add([{"role": "user", "content": "My dog's name is Charlie. Charlie is a beagle."}],
      user_id=uid)
m.add([{"role": "user", "content": "My sister Anna lives in Berlin and works at Zalando."}],
      user_id=uid)
m.add([{"role": "user", "content": "My sister lives in Munich and works at BMW."}],
      user_id=uid)

# 1) direct module: graph walk anchored on query tokens
from cortexm.experimental.graph_recall import graph_recall_boost
boosts, stats = graph_recall_boost(m.store, "Where does Anna live", uid,
                                   scope=None)
assert stats["n_query_tokens"] >= 1, stats
annas = [f for f in m.store.get_facts(list(boosts))
         if f.subject == "Anna"]
assert annas, f"Anna fact not in graph walk: {boosts}"
print("[1] graph walk stats:", stats)

# 2) coherence: facts on same day with shared entities corroborate
from cortexm.experimental.coherence import coherence_scores
facts = m.store.query_facts(user_id=uid)
coh = coherence_scores(facts, window_days=30)
assert coh, "coherence scores empty"
assert any(v > 0 for v in coh.values()), coh
print("[2] coherence scores:", {k: round(v, 2) for k, v in list(coh.items())[:6]})

# 3) end-to-end reader path: search must surface Anna's Berlin fact
res = m.search("Where does Anna live?", user_id=uid, k=5)
block = res.context_block if hasattr(res, "context_block") else str(res)
assert "Berlin" in block or "Berlin" in str([getattr(f, 'value', '') for f in getattr(res, 'facts', [])]), \
    f"Berlin not surfaced: {block[:400]}"
print("[3] e2e search OK — Berlin surfaced")

# 4) determinism: same query twice → same fact ids
r1 = m.search("Where does Anna live?", user_id=uid, k=5)
r2 = m.search("Where does Anna live?", user_id=uid, k=5)
ids1 = [f.id for f in r1.facts] if hasattr(r1, "facts") else []
ids2 = [f.id for f in r2.facts] if hasattr(r2, "facts") else []
if ids1 and ids2:
    assert ids1 == ids2, f"non-deterministic: {ids1} vs {ids2}"
    print("[4] determinism OK (same fact ordering)")
else:
    print("[4] determinism: skipped (no facts attr)")

m.close()
print("\nEXPERIMENTAL WIRING SMOKE OK")
