# Context-M — BEAM-Style Benchmark Results

## Bucket: 128K (128,056 est. tokens, 37 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **100.0%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 73.0% | 100% | 0% | 100% | 67% | 100% | 33% | 100% | 100% | 100% | 33% |
| bm25 | 73.0% | 75% | 50% | 100% | 67% | 50% | 0% | 100% | 100% | 0% | 100% |

- Ingest: 1.22s for 104,630 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 79 facts / 2,470 chunks / 24 commits (5 derived by Datalog)
- Provenance completeness: 97.3% | retrieval latency p50≈7.64ms

## Bucket: 500K (500,098 est. tokens, 72 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **100.0%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 69.4% | 88% | 25% | 100% | 58% | 100% | 17% | 100% | 100% | 100% | 17% |
| bm25 | 72.2% | 75% | 50% | 100% | 67% | 50% | 0% | 100% | 100% | 0% | 100% |

- Ingest: 4.83s for 103,564 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 171 facts / 9,522 chunks / 56 commits (9 derived by Datalog)
- Provenance completeness: 97.2% | retrieval latency p50≈8.22ms

## Bucket: 1M (1,000,100 est. tokens, 107 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **100.0%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 70.1% | 92% | 25% | 100% | 50% | 100% | 22% | 100% | 100% | 100% | 33% |
| bm25 | 71.0% | 75% | 33% | 100% | 67% | 50% | 11% | 100% | 100% | 0% | 100% |

- Ingest: 9.87s for 101,295 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 251 facts / 19,040 chunks / 88 commits (13 derived by Datalog)
- Provenance completeness: 96.3% | retrieval latency p50≈8.82ms

## Bucket: 10M (10,000,037 est. tokens, 216 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **100.0%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 65.5% | 92% | 25% | 100% | 42% | 100% | 17% | 79% | 100% | 67% | 22% |
| bm25 | 63.0% | 75% | 33% | 100% | 67% | 50% | 0% | 0% | 100% | 0% | 78% |

- Ingest: 105.78s for 94,534 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 609 facts / 190,452 chunks / 333 commits (26 derived by Datalog)
- Provenance completeness: 96.8% | retrieval latency p50≈11.7ms
