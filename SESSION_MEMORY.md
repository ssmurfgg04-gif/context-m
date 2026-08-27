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

## LLM evaluation status (IMPORTANT for next session)

- z-ai gateway: 429 quota-blocked ALL session (previous session burned
  it); probe loop ran in background — check
  `/home/z/my-project/scripts/llm_probe.log`
- Gemini API key AQ.Ab8R...OFpg: VALID but the sandbox egresses from
  Hong Kong → generativelanguage.googleapis.com refuses (region block,
  deterministic). Vertex aiplatform.googleapis.com works network-wise
  but the Agent Platform API is disabled in the key's project
  (555137337191) and enabling needs console access.
- To get the Gemini numbers: user adds GEMINI_API_KEY secret to the
  GitHub repo and runs the llm-eval workflow, OR runs from a supported
  region: `LLM_BACKEND=gemini GEMINI_API_KEY=... node
  benchmarks/llm/judge_llm.mjs benchmarks/ood/judge_items.jsonl
  benchmarks/ood/judge_items_scored_gemini.jsonl` then
  `python benchmarks/aggregate_judge.py --ood-items ... --ood-scored
  ... --out benchmarks/results`
- 58/240 OOD cross-check items already scored with glm-4-plus
  (agreement 75.9%, LLM judge grades LOWER — det judge not inflating).

## Push status

- **No GitHub PAT available this session** (previous PAT revoked/not
  stored). 7 local commits ahead of origin/main (federation, rust,
  leaderboard, llm-harness, docs). Push when a PAT is provided:
  `git push origin main`.

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
