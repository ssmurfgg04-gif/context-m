# Context-M Final Benchmark Results (v3 — 2026-08-28 Engineering Push)

**Run timestamp**: 2026-08-28T16:30:00+00:00

**Configuration**: 5 users × 50 facts/user, codec=int8, dims=768, **all features ON** (unmess + dissim + bitap + tiny_fallback + prefilter + ppr + rerank + reconstruct + mind + fade + tmt), SLB-disabled (cold-cache measurement)

## Ingest
- Wall time: 0.267s
- Facts: 173
- Throughput: 28,063 tokens/sec
- μ=0 protocol: verified (zero LLM calls)

## 1. Retrieval Speed (Latency)
| Percentile | Latency (μs) |
|---|---|
| p50 | 7241.3 |
| p95 | 8861.4 |
| p99 | 10072.0 |
| min | 6420.0 |
| max | 11200.0 |

Cold-cache, all features on (PPR + prefilter + rerank + tiny_fallback).
Warm SLB-on production latency is ~100 μs (SLB short-circuits all the new layers).

## 2. Cost per 1M Queries
- Wall time per 1M queries: 127.0 min
- Cost per 1M queries: $1.0579
- LLM calls per 1M queries: 0 (μ=0 protocol)

## 3. Storage Efficiency
- Bytes per fact: 1068.0
- Total storage: 0.18MB
- Compression vs FP32: 3.2x
- Codec: int8 (dims=768)

## 4. Context Handling
- Context block size p50: 325 tokens
- Facts returned p50: 10
- Max block size: 338 tokens

## 5. Continuous Learning / Evolution Stress Test
| Phase | Facts | Retrieval latency (ms) |
|---|---|---|
| 1 | 44 | 7.15 |
| 2 | 86 | 7.15 |
| 3 | 133 | 7.25 |
| 4 | 173 | 7.35 |

- Growth ratio over 4 phases: 3.93x (linear = 4.0)
- Memory reduction after consolidation: 43.2%
- Facts before: 183, active after: 104

## What's New vs v2

This run measures with ALL the 2026-08-28 engineering-push features
enabled at once:
- tiny_transformer_fallback (μ≈0 small-model fallback, fires on pattern miss)
- prefilter_triples (HippoRAG 2 query-aware triple filter)
- ppr_enabled (Personalized PageRank graph diffusion, on by default in prod)
- enable_rerank (cross-encoder rerank, lifts prec@5)
- reconstruct_enabled (MRAgent ICML 2026 active reconstruction path)
- mind_diversity_check (InjecMEM attack mitigation)
- fade_enabled (FadeMem forgetting, on by default in prod)
- tmt_enabled (TiMem temporal memory tree, off by default — opt-in)
- bitap_trigger_enabled (Wu-Manber fuzzy trigger widening)
- unmess_enabled (PerUserIdiolectNormalizer + DisSim compound sentence splitter)

Latency rose from 4.5ms → 7.2ms because all the new layers run per
query. Storage and consolidation numbers are unchanged because the
new layers don't touch the storage path.

The BEAM-10M prec@5 lift from the new layers: **0.9143 → 0.9429
(+2.86pp)** on top of the existing rerank stack, at the same ingest
cost (278 facts) and 0.1ms *faster* per query (prefilter shrinks the
candidate pool before fusion).
