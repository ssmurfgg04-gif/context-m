# Context-M — BEAM-Style Benchmark Results

## Bucket: 128K (128,000 est. tokens, 35 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **100.0%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 65.7% | 100% | 0% | 100% | 50% | 100% | 33% | 100% | 100% | 100% | 0% |
| bm25 | 71.4% | 75% | 50% | 100% | 67% | 50% | 0% | 100% | 100% | 0% | 100% |

- Ingest: 1.22s for 105,027 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 79 facts / 2,506 chunks / 33 commits (5 derived by Datalog)
- Provenance completeness: 100.0% | retrieval latency p50≈6.29ms

## Bucket: 500K (500,010 est. tokens, 72 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **100.0%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 63.9% | 88% | 12% | 100% | 33% | 100% | 0% | 100% | 100% | 100% | 33% |
| bm25 | 72.2% | 75% | 50% | 100% | 67% | 50% | 0% | 100% | 100% | 0% | 100% |

- Ingest: 4.66s for 107,291 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 174 facts / 9,523 chunks / 65 commits (9 derived by Datalog)
- Provenance completeness: 100.0% | retrieval latency p50≈6.79ms

## Bucket: 1M (1,001,145 est. tokens, 107 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **100.0%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 67.3% | 100% | 25% | 100% | 33% | 100% | 22% | 100% | 100% | 100% | 22% |
| bm25 | 71.0% | 75% | 33% | 100% | 67% | 50% | 11% | 100% | 100% | 0% | 100% |

- Ingest: 9.35s for 107,019 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 251 facts / 19,136 chunks / 87 commits (13 derived by Datalog)
- Provenance completeness: 100.0% | retrieval latency p50≈6.71ms

## Bucket: 10M (10,000,005 est. tokens, 212 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **97.6%** | 100% | 100% | 100% | 92% | 100% | 100% | 100% | 100% | 100% | 89% |
| vector_only | 63.4% | 92% | 21% | 100% | 33% | 100% | 17% | 71% | 100% | 100% | 22% |
| bm25 | 59.4% | 75% | 25% | 100% | 67% | 50% | 0% | 0% | 96% | 0% | 61% |

- Ingest: 98.37s for 101,661 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 598 facts / 188,933 chunks / 319 commits (30 derived by Datalog)
- Provenance completeness: 100.0% | retrieval latency p50≈7.68ms
