"""Example 12 — async LLM enrichment fallback (graceful degradation).

μ=0 ingest is deterministic and free, but English regex patterns miss
non-English, heavy slang, and indirect phrasing. The enrichment fallback
re-extracts ONLY the zero-signal chunks, post-store, off the critical
path:

  * facts carry provenance {"pattern": "llm_enrichment"} — auditable
  * confidence capped at 0.85, deterministic facts always outrank them
  * llm_calls counter increments — the μ=0 audit trail stays honest
  * injectable extractor: tests pass a fake; production uses the
    z-ai SDK bridge (benchmarks/llm/extract_facts.mjs) or any callable
"""

import datetime as dt

from cortexm import Memory

TS = dt.datetime(2026, 1, 10, tzinfo=dt.timezone.utc)

m = Memory()

# --- French + slang text the μ=0 patterns cannot catch ---------------------
m.add("Bonjour, je m'appelle Alice Dupont. Je travaille chez Vercel "
      "comme ingenieure logiciel.",
      user_id="alice", timestamp=TS)
m.add("ya i quit stripe btw, doing the netflix thing now lol",
      user_id="alice", timestamp=TS)

before = m.search("Where does Alice work now?", user_id="alice")
print("μ=0 only  ->", before["context_block"].splitlines()[1][:70])


# --- an injectable LLM extractor (use any callable with this signature) ----
def fake_llm_extractor(texts, subjects):
    """Production: subprocess to your LLM of choice. Signature:
    (texts, subject_hints) -> list[list[fact_dict]] aligned with texts."""
    out = []
    for t in texts:
        if "Vercel" in t or "Ver" in t:
            out.append([
                {"subject": "Alice Dupont", "relation": "name",
                 "value": "Alice Dupont", "confidence": 1.0},
                {"subject": "Alice Dupont", "relation": "works_at",
                 "value": "Vercel", "confidence": 0.9},
                {"subject": "Alice Dupont", "relation": "role",
                 "value": "ingenieure logiciel", "confidence": 0.8}])
        else:
            out.append([
                {"subject": "Alice Dupont", "relation": "left",
                 "value": "Stripe", "confidence": 0.9},
                {"subject": "Alice Dupont", "relation": "works_at",
                 "value": "Netflix", "confidence": 0.95}])
    return out


report = m.enrich("alice", extractor=fake_llm_extractor)
print("enrichment ->", report)

after = m.search("Where does Alice work now?", user_id="alice")
print("enriched  ->", after["context_block"].splitlines()[1][:70])

# --- provenance marks enriched facts, always --------------------------------
for f in m.store.query_facts(user_id="alice", active=True):
    pat = f.provenance.get("pattern")
    if pat == "llm_enrichment":
        print("enriched fact:", f.display()[:60], "| conf:", f.confidence)

print("llm_calls:", m.stats()["counters"]["llm_calls"],
      "(μ=0 ingest itself made zero)")
