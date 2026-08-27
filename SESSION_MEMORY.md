# Context-M — Session Memory

Living state file for autonomous continuation. Last updated: research +
hardening session (post-delivery), 2026-08-27.

## Current state: RESEARCH-GROUNDED HARDENING PASS COMPLETE

- **Repo:** `/home/z/my-project/context-m/` (sole ownership)
- **Package:** `context_m` (+ `cortexm` alias)
- **Tests:** 27/27 green (25 original + 2 MINJA tests)
- **Benchmarks (μ=0 asserted, 3 seeds — mean ± sd):**

| bucket | context_m | bm25 | vector_only |
|---|---|---|---|
| 128k | **97.1% ± 5.0%** | 68.2% | 64.5% |
| 500k | **98.6% ± 1.4%** | 70.4% | 65.0% |
| 1m | **98.8% ± 1.4%** | 66.8% | 68.8% |
| 10m | **98.3% ± 0.7%** | 58.7% | 65.4% |

10M per-seed: 97.6 / 98.2 / 99.1 (seeds 42/44/45). Eight of ten
abilities average 100.0% at 10M; IE 92.6%, TR 94.4%.

## What this session changed (web-research-driven hardening)

1. **MINJA contagion guard** (arXiv:2503.03704): second-order injection
   defense — quarantined text is a tainted corpus; re-ingest with
   ≥0.50 sentence-Jaccard overlap or a verbatim quote auto-quarantines.
   `security/injection.py: contagion_scan`, writer hook, config
   `quarantine_contagion` / `contagion_threshold`.
2. **Process-level determinism**: reader ranking tie-breaks on fact
   CONTENT (not uuid4 ids); palace `_flat_search` iterates candidates in
   palace row order + `argsort(kind="stable")`; bench runner deletes
   stale DB files before a run. Verified: identical scores across
   PYTHONHASHSEED 1/2/7/99.
3. **SLB scope-keying (correctness bug)**: the semantic lookaside buffer
   was scope-blind — user A's near-duplicate query could hit user B's
   cache entry and then die in the scope filter, returning EMPTY blocks
   (the root cause of the seed-44 EO/CR dips). Lookup/store now carry a
   scope key; note-producing intents (ordering/temporal/count) bypass
   the SLB entirely.
4. **Reader hint fixes**: `work\w*` (matches past tense "worked");
   category words (music/food/coffee/…) map to prefers/likes.
5. **Generator fix**: every preference category now gets an initial
   utterance (food prefs were previously never stated → PF probes
   unanswerable for any system).
6. **docs/RESEARCH.md** (new): literature lineage — adopted / aligned /
   rejected with reasons (BEAM, Zep, HippoRAG 2, MINJA, InjecMEM, A-MEM,
   MemOS, RaBitQ, GHRR/qFHRR, SleepGate/SCM, Synapse, NeuSymMS, AMA-Bench).
7. Multi-seed variance reporting (`benchmarks/results/variance.json`,
   aggregate script, BENCHMARKS.md + README tables).

## Decisions log (this session)

- Contagion threshold 0.50: benign common-word overlap bottoms out at
  ~0.25–0.33; regex-evading near-copies score 0.5–0.8. Deep paraphrase
  laundering documented as out of scope (honest limitation).
- Determinism via content tie-breaks, NOT content-derived ids:
  content-hashed ids would change INSERT OR REPLACE semantics and
  collide with derived facts — too risky for the value.
- The SLB bypass for ordering/temporal/count intents: those emit
  procedural notes (ORDERING:, COUNT:, temporal windows) that a cache
  hit would silently drop.

## Bug war stories (do not reintroduce)

- **SLB scope blindness**: a performance cache became a correctness bug
  the moment two users asked lexically-identical questions. Any
  result-cache keyed on content MUST key on scope too.
- **uuid4 ids leak into rankings**: any sort that tie-breaks on id is
  a hidden random shuffle. Tie-break on content or insertion order.
- **Reused bench DB dirs contaminate reruns** (facts accumulate,
  scores drift): the runner now unlinks stale DBs. When verifying
  determinism, ALWAYS use fresh db-dirs — the earlier "hash-seed
  nondeterminism" was this.
- f-string quantifier braces `{2,40}` in rf-strings; `re.M` in judge
  parser; entity-resolution seen/seen_out split; temporal_window param
  order; double rotation in codec `scores()`; batched transactions;
  bounded interference sampling (all from prior sessions, still fixed).

## Next actions

1. Publish: GitHub repo push (README, docs, benchmarks artifacts).
2. LLM reader/judge replication of canonical BEAM (`llm_judge=` slot).
3. Rust port of `vsa/codecs.py` + `vsa/index.py` behind the codec seam.
4. ArcadeDB backend behind `TraceStore`'s API.
5. Full Personalized-PageRank read mode (HippoRAG 2 lineage) — the
   entity-hop expansion is its depth-2 approximation today.
6. CRDT federated Trace sync; leaderboard site from results JSON.
7. Additional seeds (46+) for the variance table; publish per-ability
   variance too.

## Environment notes

- python3 = 3.12.14 (numpy 2.1.3, scipy 1.14.1, pytest 9.0.2, blake3
  installed via `python3 -m pip install --break-system-packages`).
  Plain `pip` targets python3.13 — always `python3 -m pip`.
- Full benchmark regeneration (3 seeds × 4 buckets + micro) ≈ 7 min.
- Bench artifact path: `benchmarks/results/` (JSON per bucket per seed,
  `variance.json`, `micro.json`, REPORT.md per seed dir).
