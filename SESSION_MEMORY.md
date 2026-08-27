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
