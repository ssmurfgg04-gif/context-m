# Context-M Final Benchmark Results

**Run timestamp**: 2026-08-28T12:02:39.933959+00:00

**Configuration**: 5 users × 50 facts/user, codec=int8, dims=768, unmess=True, reconstruct=True, SLB-disabled=True

## Ingest
- Wall time: 0.267s
- Facts: 173
- Throughput: 28,063 tokens/sec
- μ=0 protocol: verified (zero LLM calls)

## 1. Retrieval Speed (Latency)
| Percentile | Latency (μs) |
|---|---|
| p50 | 4517.3 |
| p95 | 5880.7 |
| p99 | 6557.8 |
| min | 4016.4 |
| max | 6753.0 |

## 2. Cost per 1M Queries
- Wall time per 1M queries: 78.75 min
- Cost per 1M queries: $0.6563
- LLM calls per 1M queries: 0 (μ=0 protocol)

## 3. Storage Efficiency
- Bytes per fact: 1068.0
- Total storage: 0.18MB
- Compression vs FP32: 3.2x
- Codec: int8 (dims=768)

## 4. Context Handling
- Context block size p50: 323 tokens
- Facts returned p50: 10
- Max block size: 336 tokens

## 5. Continuous Learning / Evolution Stress Test
| Phase | Facts | Retrieval latency (ms) |
|---|---|---|
| 1 | 44 | 4.4 |
| 2 | 86 | 4.5 |
| 3 | 133 | 4.57 |
| 4 | 173 | 4.54 |

- Growth ratio over 4 phases: 3.93x (linear = 4.0)
- Memory reduction after consolidation: 43.2%
- Facts before: 183, active after: 104
