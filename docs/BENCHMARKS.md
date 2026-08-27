# Context-M — Benchmark Results

Three tiers of evaluation, ordered by honesty. **Read the OOD tier first:
it measures what the system does on phrasing the extractor was not
written against.** The in-distribution tier is a regression harness whose
ceiling is structural (corpus templates and extractor patterns were
authored together — see `docs/FAILURE_MODES.md`). Methodology details in
`docs/METHODOLOGY.md`; interactive view: `leaderboard/index.html`.

## Tier 1 — Out-of-distribution benchmark (the honest number)

Ground-truth fact registries for 4 personas were re-rendered by an
independent LLM (glm-4-plus) in six styles the pattern author never saw;
probes and deterministic judge are identical to the in-distribution run,
so ID-vs-OOD deltas are apples-to-apples. Renderer omissions (3/714
facts) are excluded from extraction recall and tracked separately.

| OOD style | Extraction recall (mean ± sd over 4 personas) | End-to-end (10 abilities) | + async LLM enrichment |
|---|---|---:|---:|
| paraphrase | 0.094 ± 0.094 | 0.282 | — |
| negation | 0.756 ± 0.033 | 0.693 | 0.657 |
| indirect speech | 0.449 ± 0.102 | 0.486 | 0.493 |
| informal/slang | 0.051 ± 0.059 | 0.150 | 0.171 |
| non-English | 0.000 ± 0.000 | 0.157 | 0.164 |
| code-switching | 0.579 ± 0.181 | 0.607 | 0.586 |

Key readings:

* **The generalization gap is real and large.** In-distribution 100% →
  OOD paraphrase 9-28%. Non-English ingest is zero without the LLM
  fallback. See `docs/FAILURE_MODES.md` for per-fact-type recall and
  worked failure examples.
* **Async LLM enrichment is not a rescue** in its current form: +1-2
  points on the hardest styles, −4 points on negation. It surfaces facts
  but does not reconstruct the bi-temporal chains the contradiction
  engine and temporal probes need.
* The VSA layer keeps e2e above extraction recall on every style —
  lexical holograms buy partial credit even when extraction fails.

Reproduce: `python benchmarks/run_ood_pipeline.py --personas 4`.
Artifacts: `benchmarks/results/ood/*.json`, rendered corpora with
conveyance tracking in `benchmarks/ood/rendered_p4.jsonl`.

### LLM-judge cross-check (canonical-protocol replication)

240 probe/context pairs were exported in the BEAM judge format and graded
by two independent LLM judges. Judge models are NOT gpt-5, so these are
cross-checks on our own numbers, not BEAM-comparable scores.

**Canonical sweep — `gemini-3.5-flash-lite` from a clean CI runner
([.github/workflows/llm-eval.yml](../.github/workflows/llm-eval.yml)),
237/240 items:**

| Grader | Mean | Exact agreement | Within ½ point |
|---|---:|---:|---:|
| Deterministic nugget judge | 0.335 | 82.7% | 87.8% |
| LLM judge (gemini-3.5-flash-lite) | 0.222 | — | — |

**Second judge — glm-4-plus (58-item quota sample from an earlier run):**

| Grader | Mean | Exact agreement | Within ½ point |
|---|---:|---:|---:|
| Deterministic nugget judge | 0.345 | 75.9% (44/58) | 77.6% (45/58) |
| LLM judge (glm-4-plus) | 0.250 | — | — |

**Finding:** both LLM judges grade the same contexts *lower* than the
deterministic judge — the OOD numbers above are, if anything, slightly
generous relative to independent graders. Two judge models, two samples,
same direction. **Reproducibility:** a second, independent CI run of the
Gemini sweep produced a byte-identical scored file (same 237 scores) —
the judge is deterministic at temperature 0. Artifacts:
`benchmarks/results/ood/llm_judge_crosscheck_gemini.json` (full sweep),
`benchmarks/results/ood/llm_judge_crosscheck.json` (glm sample),
`benchmarks/results/llm_eval_summary.md`.

### Real-GitHub track (LLM reference + LLM-judged QA)

Ran end-to-end in CI with `gemini-3.5-flash-lite` as both reference
extractor and QA judge (5 threads, 150 comments):

- μ=0 extractor: 16 facts, 1.1 ms/comment, $0.00
- LLM reference extractor: 158 facts, 2,779 ms/comment, ~90K tokens
- μ=0 recall vs LLM reference: **0.6%**; precision 6.3%
- Retrieval QA (19 questions): overall 0.263, answerable 0.067,
  abstention 1.0

The μ=0 path is ~2,500× faster and free, but on real developer-issue
language it captures ~10× fewer facts than an LLM extractor — that is
the honest cost/coverage frontier. Artifacts:
`benchmarks/results/real_github/`.

## Tier 2 — In-distribution (regression harness)

BEAM-style synthetic benchmark (methodology mirrors arXiv:2510.27246;
see `docs/METHODOLOGY.md`). All runs: seed 42, μ=0 protocol asserted
(zero LLM calls for ingest, retrieval, and judging), BLAKE3 provenance
verified on every retrieval.

**Caveat, stated up front:** the corpus generator and the extractor
patterns were authored against the same template families. This tier
measures template coverage — its job is regression detection ("did we
break template extraction?"), not capability. It is NOT comparable to
canonical BEAM SOTA (Exabase M-1, 68.0%): different corpus, judge, and
protocol.

## Headline

| Bucket | Est. tokens | Questions | Context-M | BM25-RAG | Vector-only |
|---|---:|---:|---:|---:|---:|
| 128K | 128,099 | 35 | **100.0%** | 71.4% | 65.7% |
| 500K | 500,002 | 72 | **100.0%** | 72.2% | 63.9% |
| 1M | 1,001,133 | 107 | **100.0%** | 71.0% | 67.3% |
| 10M | 10,000,078 | 212 | **100.0%** | 62.3% | 63.4% |

**Strategic context, honestly framed:** the plan's target was 70%+ at
BEAM-10M and the cited August-2026 SOTA is Exabase M-1 at 68.0%
(LLM-in-loop ingest). The in-distribution table clears both — but that
comparison is **not apples-to-apples** (different corpus, deterministic
vs gpt-5 judge, template-matched evaluation), which is exactly why the
headline framing moved to the OOD tier above. What IS defensible from
this tier: $0 LLM spend, 100K tokens/s ingest, and sublinear memory
growth at 10M tokens.

## Seed variance (5 seeds: 42 / 44 / 45 / 46 / 47)

Single-seed scores are fragile; the table above is seed 42. Across five generator seeds — including two (46, 47) that were never inspected during development — the score is stable:

| Bucket | Questions | Context-M (mean ± sd) | BM25-RAG | Vector-only |
|---|---:|---:|---:|---:|
| 128K | 37 | **100.0% ± 0.0%** | 70.2% | 69.0% |
| 500K | 72 | **100.0% ± 0.0%** | 70.5% | 67.9% |
| 1M | 107 | **100.0% ± 0.0%** | 68.8% | 70.1% |
| 10M | 216 | **100.0% ± 0.0%** | 61.6% | 66.1% |

Per-seed Context-M scores, 10M bucket: seed 42 = 100.0%, seed 44 = 100.0%, seed 45 = 100.0%, seed 46 = 100.0%, seed 47 = 100.0%.

## Bucket 128K

Corpus: 128,099 estimated tokens, 32 sessions, 1 personas, 2,507 messages.

| System | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR | Overall |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **context_m** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | **100.0%** |
| vector_only | 100% | 0% | 100% | 50% | 100% | 33% | 100% | 100% | 100% | 0% | 65.7% |
| bm25 | 75% | 50% | 100% | 67% | 50% | 0% | 100% | 100% | 0% | 100% | 71.4% |

**Ingest (μ=0):** 1.24s for 128,099 tokens — **103,454 tokens/s**, 2,024.7 messages/s, **0 LLM calls** (protocol: verified).

**Memory:** 79 facts (77 active) from 2,507 chunks — memory grows *sublinearly* with conversation length because repeated noise dedupes. 33 hash-chained commits; 5 facts derived by the Datalog engine.

**Trust:** provenance completeness 97.1% (every retrieved fact hash-verified against its source), audit latency 7.6 ms, hash provider `blake3-256`, codec `int8`, VSA mode `perm`.

## Bucket 500K

Corpus: 500,002 estimated tokens, 64 sessions, 2 personas, 9,523 messages.

| System | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR | Overall |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **context_m** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | **100.0%** |
| vector_only | 88% | 12% | 100% | 33% | 100% | 0% | 100% | 100% | 100% | 33% | 63.9% |
| bm25 | 75% | 50% | 100% | 67% | 50% | 0% | 100% | 100% | 0% | 100% | 72.2% |

**Ingest (μ=0):** 4.89s for 500,002 tokens — **102,225 tokens/s**, 1,947.0 messages/s, **0 LLM calls** (protocol: verified).

**Memory:** 174 facts (162 active) from 9,523 chunks — memory grows *sublinearly* with conversation length because repeated noise dedupes. 65 hash-chained commits; 9 facts derived by the Datalog engine.

**Trust:** provenance completeness 97.2% (every retrieved fact hash-verified against its source), audit latency 8.24 ms, hash provider `blake3-256`, codec `int8`, VSA mode `perm`.

## Bucket 1M

Corpus: 1,001,133 estimated tokens, 86 sessions, 3 personas, 19,136 messages.

| System | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR | Overall |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **context_m** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | **100.0%** |
| vector_only | 100% | 25% | 100% | 33% | 100% | 22% | 100% | 100% | 100% | 22% | 67.3% |
| bm25 | 75% | 33% | 100% | 67% | 50% | 11% | 100% | 100% | 0% | 100% | 71.0% |

**Ingest (μ=0):** 9.9s for 1,001,133 tokens — **101,151 tokens/s**, 1,933.4 messages/s, **0 LLM calls** (protocol: verified).

**Memory:** 251 facts (236 active) from 19,136 chunks — memory grows *sublinearly* with conversation length because repeated noise dedupes. 87 hash-chained commits; 13 facts derived by the Datalog engine.

**Trust:** provenance completeness 96.3% (every retrieved fact hash-verified against its source), audit latency 8.42 ms, hash provider `blake3-256`, codec `int8`, VSA mode `perm`.

## Bucket 10M

Corpus: 10,000,078 estimated tokens, 318 sessions, 6 personas, 188,936 messages.

| System | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR | Overall |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **context_m** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | **100.0%** |
| vector_only | 92% | 21% | 100% | 33% | 100% | 17% | 71% | 100% | 100% | 22% | 63.4% |
| bm25 | 75% | 25% | 100% | 67% | 50% | 0% | 0% | 96% | 0% | 94% | 62.3% |

**Ingest (μ=0):** 105.19s for 10,000,078 tokens — **95,065 tokens/s**, 1,796.1 messages/s, **0 LLM calls** (protocol: verified).

**Memory:** 594 facts (508 active) from 188,936 chunks — memory grows *sublinearly* with conversation length because repeated noise dedupes. 319 hash-chained commits; 26 facts derived by the Datalog engine.

**Trust:** provenance completeness 97.2% (every retrieved fact hash-verified against its source), audit latency 9.65 ms, hash provider `blake3-256`, codec `int8`, VSA mode `perm`.

## Micro-benchmarks

### Retrieval latency & index scaling

| Vectors | Flat scan | Tree p50 | Tree p99 | Quality ratio* | Build |
|---:|---:|---:|---:|---:|---:|
| 10000 | 22.87 ms | 0.957 ms | 2.156 ms | 0.8526 | 0.73 s |
| 50000 | 109.32 ms | 1.519 ms | 3.115 ms | 0.8065 | 3.06 s |
| 100000 | 213.45 ms | 1.248 ms | 2.669 ms | 0.7753 | 4.46 s |

*quality ratio = mean score of tree top-10 ÷ mean score of brute-force top-10 (membership differs when neighbors are near-tied; retrieval quality is what agents consume). The plan's milestone was <1 ms retrieval at 100K memories: tree p50 = 1.248 ms.

### Codec ablation (cortexm-compress tiers)

| Codec | Bytes/vector | 1M memories | Self-hit@10 | Overlap@10 vs FP32 | Recall@10 in top-50 |
|---|---:|---:|---:|---:|---:|
| `int8` | 770 | 770 MB | 1.0 | 0.9025 | 1.0 |
| `binary` | 96 | 96 MB | 1.0 | 0.419 | 1.0 |
| `rabitq` | 96 | 96 MB | 1.0 | 0.433 | 1.0 |
| `pq` | 8 | 8 MB | 0.865 | 0.2675 | 0.9995 |

Reading: int8 is the near-lossless workhorse; binary/rabitq/PQ are *shortlist* codecs — they recover the full-precision top-10 inside their top-50 at ~1.00, then symbolic fusion (which does not depend on vector precision) ranks the final answer set. That is the edge-tier design: 96 B or 8 B per memory with the symbolic Trace as the precision anchor.

### Self-healing memory (binary HDC + TMR)

| Corruption | plain binary | with TMR |
|---:|---:|---:|
| 0% bit flips | 1.0 | 1.0 |
| 1% bit flips | 1.0 | 1.0 |
| 5% bit flips | 1.0 | 1.0 |
| 10% bit flips | 1.0 | 1.0 |
| 20% bit flips | 0.96 | 1.0 |

Self-identification = a corrupted hypervector still recognizes itself among 5,000 stored vectors. Binary HDC tolerates up to ~10% bit corruption at 100%; the Trace-side hash check plus re-encoding heals beyond the correction radius (`examples/09_self_healing.py`).

### Semantic Lookaside Buffer

Conversational replay: hit rate **70%**, hit latency 6.7 µs vs miss 6.3 µs (64-entry ring, 0.97 similarity threshold).

### μ=0 cost asymmetry

| | Per memory | 1M memories |
|---|---:|---:|
| LLM-in-loop ingest (competitor) | $0.001 | $1,000 |
| Context-M μ=0 ingest (CPU only) | $0.00001 | $10 |

A 100× structural cost advantage that cannot be copied without rewriting the ingest path.

## Rust acceleration scorecard

`benchmarks/rust_vs_numpy.py` → `benchmarks/results/rust_accel.json`
(Xeon, AVX2+FMA runtime dispatch, release/lto):

| Hot path | Python/NumPy | Rust wheel | Speedup |
|---|---:|---:|---:|
| h64 feature hash (×200) | 180.5 µs | 83.9 µs | **2.2×** |
| VSA bind (permutation) | 4.59 µs | 1.34 µs | **3.4×** |
| encode_fact (fused) | 31.23 µs | 6.47 µs | **4.8×** |
| SLB hit (64×768) | 4.89 µs | 4.78 µs | 1.0× (tie) |

The SLB tie is a finding, not a failure: BLAS-backed NumPy is already
optimal at this size, and we publish it as such. h64 parity is
byte-exact (hash keys must never diverge). Float kernels use explicit
AVX2 intrinsics with runtime detection — LLVM does not auto-vectorize
float reductions (FP addition is not associative), and the scalar
fallback keeps the wheels portable.

### Quadrant — page-clustered log-depth index

20,000 vectors × 768d, page capacity 64 → 529 pages, tree depth 13.
Clustered corpus = realistic hologram geometry (shared bound components):

| Config | Recall@10 | Node visits | Latency |
|---|---:|---:|---:|
| max_leaves=1 (pure descent) | 0.39 | 12 | 34 µs |
| max_leaves=4 | 0.64 | 17 | 82 µs |
| max_leaves=16 | **0.97** | 32 | 238 µs |
| exact NumPy brute force | 1.00 | — | 1679 µs |

97% recall at 7.0× brute-force speed, touching 32 of 529 pages — the
O(log N) descent is instrumented (visit counts per query), not asserted.
**Adversarial row**: on structure-free random corpora the same index
collapses to 0.12–0.19 recall — without cluster structure a pruned index
cannot beat brute force. Published, not hidden; quadrant is opt-in for
the L2 palace, the default path remains exact.

## CRDT federation

`benchmarks/federation_bench.py` → `benchmarks/results/federation.json`:
3 nodes × 5,000 disjoint facts (64-bucket digests, HMAC envelopes):

- initial full-mesh sync: **byte-exact convergence** in 1 gossip round
  (1.8 s, 10,491 keys/node after SINGLE_VALUED version collapse);
- partition ({A,B} | C) with 500 divergent writes/side + 50 retractions:
  heals to **byte-exact convergence**, all retractions honored on every
  node;
- new-node join: one digest + one delta (3.6 MB — all buckets diverge
  for a fresh node);
- honest caveat: 550 writes scattered uniformly across the keyspace
  touch most of the 64 buckets, so that heal shipped 17.7 MB (~full
  state). Bucket count is the granularity dial (256/1024 buckets shrink
  deltas at digest cost); write-local deployments get proportional
  deltas by construction.

---

Reproduce: `python -m context_m.bench.run --buckets 128k,500k,1m,10m` and `python -m context_m.bench.run --micro`. Runs are deterministic for a given seed and process-independent (score ties break on fact content, not random ids). Full JSON: `benchmarks/results/`.
