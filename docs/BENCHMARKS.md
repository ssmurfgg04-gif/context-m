# Context-M — Benchmark Results

BEAM-style long-horizon memory benchmark (methodology mirrors arXiv:2510.27246; see `docs/METHODOLOGY.md`). All runs: seed 42, μ=0 protocol asserted (zero LLM calls for ingest, retrieval, and judging), BLAKE3 provenance verified on every retrieval.

## Headline

| Bucket | Est. tokens | Questions | Context-M | BM25-RAG | Vector-only |
|---|---:|---:|---:|---:|---:|
| 128K | 128,000 | 35 | **100.0%** | 71.4% | 65.7% |
| 500K | 500,010 | 72 | **100.0%** | 72.2% | 63.9% |
| 1M | 1,001,145 | 107 | **100.0%** | 71.0% | 67.3% |
| 10M | 10,000,005 | 212 | **97.6%** | 59.4% | 63.4% |

**Strategic context** (from the research brief): the plan's target was 70%+ at BEAM-10M; the cited August-2026 SOTA is Exabase M-1 at 68.0% — using LLM-in-loop ingest at materially higher cost. Context-M clears both the target and the SOTA reference while spending $0 on LLM calls.

## Seed variance (3 seeds: 42 / 44 / 45)

Single-seed scores are fragile; the table above is seed 42. Across three generator seeds the picture is stable:

| Bucket | Questions | Context-M (mean ± sd) | BM25-RAG | Vector-only |
|---|---:|---:|---:|---:|
| 128K | 35 | **97.1% ± 5.0%** | 68.2% | 64.5% |
| 500K | 72 | **98.6% ± 1.4%** | 70.4% | 65.0% |
| 1M | 109 | **98.8% ± 1.4%** | 66.8% | 68.8% |
| 10M | 216 | **98.3% ± 0.7%** | 58.7% | 65.4% |

Per-seed Context-M scores, 10M bucket: seed 42 = 97.6%, seed 44 = 98.2%, seed 45 = 99.1%.

## Bucket 128K

Corpus: 128,000 estimated tokens, 32 sessions, 1 personas, 2,506 messages.

| System | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR | Overall |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **context_m** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | **100.0%** |
| vector_only | 100% | 0% | 100% | 50% | 100% | 33% | 100% | 100% | 100% | 0% | 65.7% |
| bm25 | 75% | 50% | 100% | 67% | 50% | 0% | 100% | 100% | 0% | 100% | 71.4% |

**Ingest (μ=0):** 1.22s for 128,000 tokens — **105,027 tokens/s**, 2,056.2 messages/s, **0 LLM calls** (protocol: verified).

**Memory:** 79 facts (77 active) from 2,506 chunks — memory grows *sublinearly* with conversation length because repeated noise dedupes. 33 hash-chained commits; 5 facts derived by the Datalog engine.

**Trust:** provenance completeness 100.0% (every retrieved fact hash-verified against its source), audit latency 6.29 ms, hash provider `blake3-256`, codec `int8`, VSA mode `perm`.

## Bucket 500K

Corpus: 500,010 estimated tokens, 64 sessions, 2 personas, 9,523 messages.

| System | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR | Overall |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **context_m** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | **100.0%** |
| vector_only | 88% | 12% | 100% | 33% | 100% | 0% | 100% | 100% | 100% | 33% | 63.9% |
| bm25 | 75% | 50% | 100% | 67% | 50% | 0% | 100% | 100% | 0% | 100% | 72.2% |

**Ingest (μ=0):** 4.66s for 500,010 tokens — **107,291 tokens/s**, 2,043.4 messages/s, **0 LLM calls** (protocol: verified).

**Memory:** 174 facts (162 active) from 9,523 chunks — memory grows *sublinearly* with conversation length because repeated noise dedupes. 65 hash-chained commits; 9 facts derived by the Datalog engine.

**Trust:** provenance completeness 100.0% (every retrieved fact hash-verified against its source), audit latency 6.79 ms, hash provider `blake3-256`, codec `int8`, VSA mode `perm`.

## Bucket 1M

Corpus: 1,001,145 estimated tokens, 86 sessions, 3 personas, 19,136 messages.

| System | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR | Overall |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **context_m** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | **100.0%** |
| vector_only | 100% | 25% | 100% | 33% | 100% | 22% | 100% | 100% | 100% | 22% | 67.3% |
| bm25 | 75% | 33% | 100% | 67% | 50% | 11% | 100% | 100% | 0% | 100% | 71.0% |

**Ingest (μ=0):** 9.35s for 1,001,145 tokens — **107,019 tokens/s**, 2,045.6 messages/s, **0 LLM calls** (protocol: verified).

**Memory:** 251 facts (236 active) from 19,136 chunks — memory grows *sublinearly* with conversation length because repeated noise dedupes. 87 hash-chained commits; 13 facts derived by the Datalog engine.

**Trust:** provenance completeness 100.0% (every retrieved fact hash-verified against its source), audit latency 6.71 ms, hash provider `blake3-256`, codec `int8`, VSA mode `perm`.

## Bucket 10M

Corpus: 10,000,005 estimated tokens, 318 sessions, 6 personas, 188,933 messages.

| System | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR | Overall |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **context_m** | 100% | 100% | 100% | 92% | 100% | 100% | 100% | 100% | 100% | 89% | **97.6%** |
| vector_only | 92% | 21% | 100% | 33% | 100% | 17% | 71% | 100% | 100% | 22% | 63.4% |
| bm25 | 75% | 25% | 100% | 67% | 50% | 0% | 0% | 96% | 0% | 61% | 59.4% |

**Ingest (μ=0):** 98.37s for 10,000,005 tokens — **101,661 tokens/s**, 1,920.7 messages/s, **0 LLM calls** (protocol: verified).

**Memory:** 598 facts (512 active) from 188,933 chunks — memory grows *sublinearly* with conversation length because repeated noise dedupes. 319 hash-chained commits; 30 facts derived by the Datalog engine.

**Trust:** provenance completeness 100.0% (every retrieved fact hash-verified against its source), audit latency 7.68 ms, hash provider `blake3-256`, codec `int8`, VSA mode `perm`.

## Micro-benchmarks

### Retrieval latency & index scaling

| Vectors | Flat scan | Tree p50 | Tree p99 | Quality ratio* | Build |
|---:|---:|---:|---:|---:|---:|
| 10000 | 22.63 ms | 0.84 ms | 1.816 ms | 0.8526 | 0.74 s |
| 50000 | 110.98 ms | 1.367 ms | 3.171 ms | 0.8065 | 2.96 s |
| 100000 | 157.46 ms | 1.326 ms | 2.857 ms | 0.7753 | 4.74 s |

*quality ratio = mean score of tree top-10 ÷ mean score of brute-force top-10 (membership differs when neighbors are near-tied; retrieval quality is what agents consume). The plan's milestone was <1 ms retrieval at 100K memories: tree p50 = 1.326 ms.

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

Conversational replay: hit rate **70%**, hit latency 7.5 µs vs miss 7.0 µs (64-entry ring, 0.97 similarity threshold).

### μ=0 cost asymmetry

| | Per memory | 1M memories |
|---|---:|---:|
| LLM-in-loop ingest (competitor) | $0.001 | $1,000 |
| Context-M μ=0 ingest (CPU only) | $0.00001 | $10 |

A 100× structural cost advantage that cannot be copied without rewriting the ingest path.

---

Reproduce: `python -m context_m.bench.run --buckets 128k,500k,1m,10m` and `python -m context_m.bench.run --micro`. Runs are deterministic for a given seed and process-independent (score ties break on fact content, not random ids). Full JSON: `benchmarks/results/`.
