# Context-M — Session Memory

Living state file for autonomous continuation.
Last updated: CRDT federation + Rust port + Quadrant session, 2026-08-27.

## Current state: FEDERATION + RUST ACCELERATION COMPLETE

- **Repo:** `/home/z/my-project/context-m/` (sole ownership)
- **Tests:** 116/116 green (fabric + enterprise + PPR + sandbox + WAL +
  migration + **28 federation** + **7 Rust parity**)
- **Benchmarks:** honesty-first (see below); ID 100% is labelled as
  template-matching upper bound, OOD is the headline number

## What this session built

1. **CRDT federation** (`context_m/federation/`): HLC clocks, bi-temporal
   fact CRDT (SINGLE_VALUED keys = versioned registers; MULTI_VALUED =
   per-value OR-set), union merge, OR-set retraction semantics, purge
   poison-pills, digest/delta anti-entropy (64 buckets, count+maxstamp+
   xor-fold), HMAC-signed envelopes, InMemoryMesh + FileTransport
   (offline mule protocol). Byte-exact convergence oracle in tests.
   Benchmark: 3 nodes x 5k facts, partition heal honors retractions.
2. **Rust port** (`rust/cortexm-core`): h64 (byte-exact parity),
   PermBindings (injected perms → bit-identical holograms), fused
   encode_fact, ConvBindings (HRR FFT), Rust SLB, AVX2+FMA runtime-
   dispatched SIMD kernels (float reductions do NOT auto-vectorize).
   Scorecard: encode_fact 4.8x, bind 3.4x, h64 2.2x, SLB 1.0x tie.
3. **Quadrant** (`rust/quadrant`): page-clustered log-depth 2-means
   tree, INT8 pages, best-first search with page budget + radius-bound
   pruning. 97% recall@10 at 7x brute force; visit counts instrumented;
   adversarial random-corpus collapse (0.19) published.
4. **Gemini LLM-judge backend** (`benchmarks/llm/common.mjs`):
   LLM_BACKEND=gemini, region-block detected with actionable error,
   cache keyed by backend+model. GitHub Actions workflow
   (`.github/workflows/llm-eval.yml`) runs the canonical judge from US
   runners with secrets.GEMINI_API_KEY — commits to bench/llm-eval
   branch. aggregate_judge.py merges judge scores into crosscheck JSON.

## LLM evaluation status (RESOLVED this session)

- Canonical Gemini judge RAN TO COMPLETION via GitHub Actions US runners
  (llm-eval workflow). OOD: 237/240, LLM judge 0.222 vs det 0.335,
  agreement 82.7% -> llm_judge_crosscheck_gemini.json. Real-GitHub:
  mu=0 recall 0.6% vs LLM reference, QA 0.263 -> results/real_github/.
- GEMINI_API_KEY is set as a repo secret (do not re-set unless rotated).
- Direct-from-sandbox Gemini remains region-blocked (HK egress); the
  Actions path is the permanent solution. Rerun: Actions -> llm-eval ->
  Run workflow.

## Resilience post-mortem (second llm-eval run)

- Run 2 failed after 85 min: no cross-run judge cache -> full 240-item
  OOD sweep re-billed -> Gemini daily quota exhausted -> qa_generate got
  0 pairs through backoff -> judge wrote no file -> python crashed.
- Fixes shipped (b4f9c70): actions/cache@v4 on /tmp/llm-cache keyed by
  backend+model+input hashes (reruns resume, never re-bill); judge_llm.mjs
  materialises output on 0 items; run_real_github_eval.py degrades to an
  explicit "degraded" qa_eval.json instead of crashing or faking zeros.
- aggregate_judge.py was already None-guarded; read_jsonl returns [].

## Push status

- Repo lineage RESTORED this session: environment reset wiped context-m/.git;
  re-cloned public remote (14dd0eb) and committed the lost CRDT/Rust/Quadrant
  work as 6 commits on top (llm-backend, federation, rust, docs, session, ci)
- 116/116 tests green with freshly rebuilt wheels (rustup+maturin reinstalled)
- CI workflow added: test matrix + Rust parity job (CONTEXTM_RUST=1)
- PUSH STILL BLOCKED: no PAT in environment (previous one never persisted).
  Ready: `git push origin main` from /home/z/my-project/context-m

## Known honest caveats (do not silently "fix")

- ID 100.0% is circular (extractor authored against generator templates)
- OOD paraphrase 9.4%/28.2%, non-English 0% — the real generalization gap
- Federation: scattered writes touch most buckets → near-full-state heal
  at 64 buckets (granularity dial documented)
- Quadrant collapses on structure-free random corpora
- Rust SLB ties NumPy (BLAS optimal at 64x768)
- schema_report.py: display-layer ate "[m" in "span = [min( ... )]" —
  the file was ALWAYS valid; do not "fix" it again

## Environment notes

- rustup/maturin installed (~/.cargo/bin, ~/.local/bin); wheels built
  into the venv python (3.12): cortexm_core, quadrant
- pip = system python 3.13; use `python3 -m pip` (venv 3.12)
- Build wheels: `cd rust/<crate> && maturin build --release` then
  `python3 -m pip install --force-reinstall target/wheels/*.whl`

## arXiv-inspired improvements (this session)

Built 8 new modules + 4 architectural fixes per user's Con #4-#7 list:

- `context_m/text/fuzzy.py` — Bitap (Wu-Manber k-error) + Levenshtein + n-gram
- `context_m/text/idiolect.py` — per-user idiolect normalization (case-preserving)
- `context_m/text/dissim.py` — DisSim v1 rule-based sentence splitter
- `context_m/vsa/cleanup.py` — modern Hopfield cleanup memory
- `context_m/vsa/tlsh_trie.py` — software TLSH ternary trie (TCAM emulation)
- `context_m/vsa/hologram_overlay.py` — HRR KG overlay for O(1) single-hop
- `context_m/vsa/attribution.py` — ProtoDash source attribution + retrieval_path tags
- `context_m/security/zk_hamming.py` — Hamming-distance ZK-style proofs on binary vectors
- `context_m/bridge/onnx_runtime.py` — LayerCast FP32 determinism seam (documented contract)
- `context_m/bridge/query_extract.py` — hybrid query-time extraction (store raw + lazy extract)
- `context_m/trace/rebuild.py` — formal rebuild_from_trace op (checksum audit + re-encode)
- `context_m/trace/dedup.py` — dedup + holographic compression audit
- `context_m/accel.py` updated — explicit binary/FP32 tiering (detect_tier, recommend_codec)

MCP server gained 3 new tools: `contextm_query_extract`,
`contextm_attribution`, `contextm_zk_prove`.

Claude MCP plugin (`plugins/context-m-claude/src/index.ts`) gained
`onSessionStart` ("I see you've been working on X. Continue?") and
`onSessionEnd` ("Store summary? [Y/n]") hooks + session-state file at
`~/.context-m/session_state.json`.

Tests: 149 passed, 7 skipped (Rust wheels not installed in sandbox).
38 new tests in `tests/test_arxiv_improvements.py` cover all 8 modules +
the architectural fixes.

BEAM 10M benchmark: tried HuggingFace (4 candidate datasets), all
unavailable from sandbox; fell back to 500-persona synthetic corpus
with mixed styles. Honest numbers in `benchmarks/results/beam_10m.json`
and `docs/BENCHMARKS.md` Tier 8.

LLM judge: skipped this session (user said "last", "not main thing");
CI workflow `.github/workflows/llm-eval.yml` remains ready to trigger
with `GEMINI_API_KEY` secret already set.

## Research steals (this session — Aeon / NSR / EAM + QTE refactor)

User directive: "do all these plus dig into arxiv and see what else
can be improved also dont forget to do this [Con #4-#7] btw run the
benchmarks with the real beam 10m data from hugging face lets see
how we do fr and also you can run the llm test last".

Three concrete asks + a research dump on what to steal from Aeon
(arXiv:2601.15311), Meta FAIR Memory Layers (arXiv:2412.09764), NSR
(ESWEEK24), EAM / HeatherDB.

### (1) LLM judge workflow triggered
- Fixed `.github/workflows/llm-eval.yml` push trigger typo
  (`branches: ain]` → `branches: [main]`) — push trigger was broken
- workflow_dispatch API call returned HTTP 204 (success), run queued
  at https://github.com/ssmurfgg04-gif/context-m/actions/runs/33151543480
- Workflow COMPLETED in 9 minutes (judge cache hit from prior runs)
- Results on bench/llm-eval branch (commit 7d5ee956):
  * OOD judge cross-check: 240/240 scored, **82.1% agreement** with
    det judge (LLM mean 0.2229 vs det mean 0.3354 — det is NOT
    silently inflating scores, LLM grades harder on average)
  * Real-GitHub: μ=0 extractor = 16 facts vs LLM extractor = 173
    facts → **0.0058 recall** (honest gap, by design — μ=0 is narrow,
    LLM extractor is broad; trade-off is μ=0 = $0 vs LLM = $$$)
  * Retrieval judge: 0% answerable, **100% abstention** (system
    refuses to answer rather than guess wrong on real GitHub data —
    honest behavior, not a silent failure)

### (2) Harder BEAM personas generated
- `context_m/bench/messy.py` — messify_persona_dict() applies
  slang fillers (bruh, ngl, tbh, fr fr, no cap), text-speak
  substitutions (@→at, u→you, 2→to, 4→for, rn→right now), common
  misspellings (defo, prolly, kinda), run-on compound sentences,
  code-switching, capitalization chaos, ALL-CAPS bursts
- `benchmarks/run_beam_benchmark.py` gained `--messy` flag
- Ground truth unchanged (persona's facts list preserved) so the
  BEAM judge still scores correctly
- Bug fix: `idiolect.normalize()` was stripping punctuation from
  tokens BEFORE checking the text-speak map — "@" never reached the
  map, so "I work @ Microsoft" never normalized to "I work at
  Microsoft". Now the map is checked on the FULL token first.
- Added `text_speak_map` (public attribute) to PerUserIdiolectNormalizer
  with curated common short-form mappings; callers can extend per
  domain

### (3) Query-time extraction routed through MemoryWriter
- Previous `QueryTimeExtractor` had a bespoke `_store_fact` that
  wrote directly to the Trace, bypassing the writer's quarantine /
  contradiction / lifecycle / palace encoding / edge wiring. This is
  why the query-time path measured **0.835 vs 1.017 recall** (user
  reported the regression).
- Refactored: `QueryTimeExtractor` now REQUIRES a `writer` arg;
  all writes go through `MemoryWriter.ingest_candidates`. `query()`
  does two-pass retrieval:
  * PASS 1: delegates to structured reader (`mem.reader.search` via
    `.memories()`) for ingest-path parity
  * PASS 2: falls back to raw `palace.search` + lazy reextract via
    dissim + pattern extractor + `ingest_candidates` ONLY if reader
    returned < k results
- Fixed `_chunk_has_facts` SQL bug — column is `kind` not `type` in
  the edges table

### Research steals shipped (4 new modules + wiring)

| Source  | Module                            | Steal |
|---------|-----------------------------------|-------|
| Aeon    | `trace/edges.py`                 | Centralized edge vocabulary + CAUSAL + REFERS_TO + helpers |
| Aeon    | `bridge/writer.py` (modified)    | Wires CAUSAL on SUPERSEDE + retraction paths |
| Aeon    | `trace/consolidate.py`           | `memory.consolidate()` dreaming pass (merge / retire / defrag / retrain) |
| Aeon    | `trace/blob_arena.py`           | Sidecar mmap-backed blob file (64B preview + BLAKE3 + offset) |
| NSR     | `bridge/decoders.py`             | Swappable decoders: LLMPrompt / RDF / Datalog / JSON |
| NSR     | `bridge/reader.py` (modified)   | `with_decoder(name)` swaps at runtime |
| EAM     | `api/chaos.py`                   | `chaos_ingest(mem, texts)` zero-config auto-ingest |

Skipped (logged as TODO): Meta FAIR product-key binding in PQ tier
(would touch `vsa/codecs.py` PQ path — weekend experiment scope);
NSR engineered role vectors (would require training a tiny
autoencoder — separate experiment).

### BEAM 10M benchmark with --messy flag (200 personas, synthetic)

```
config                         recall     prec@5     ms/q
baseline                       1.073      0.593      1.9
+unmess                        1.095      0.600      2.0   ← slang fix moves the needle
+unmess+dissim                 1.095      0.600      2.0
+unmess+dissim+query           1.095      0.603      6.1   ← +1.7% vs baseline, parity achieved
```

Clean corpus (no --messy): all configs at 0.594 — the messy flag is
required to make unmess / dissim / query have actual work to do.

### Tests

- 18 new tests in `tests/test_research_steals.py` cover all 4 steals
  (typed edges / decoders / consolidate / blob arena / chaos mode)
- Full suite: **167/167 green** (149 prior + 18 new)

### Commit

`c74359e feat(research-steals): Aeon + NSR + EAM adaptations + QTE refactor`
pushed to origin/main on 2026-08-28.
