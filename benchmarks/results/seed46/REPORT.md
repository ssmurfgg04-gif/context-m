# Context-M — BEAM-Style Benchmark Results

## Bucket: 128K (128,059 est. tokens, 37 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **100.0%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 75.7% | 75% | 50% | 100% | 50% | 100% | 67% | 50% | 100% | 100% | 67% |
| bm25 | 70.3% | 75% | 25% | 100% | 67% | 50% | 0% | 100% | 100% | 0% | 100% |

- Ingest: 1.24s for 103,270 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 76 facts / 2,441 chunks / 27 commits (5 derived by Datalog)
- Provenance completeness: 97.3% | retrieval latency p50≈7.67ms

## Bucket: 500K (500,067 est. tokens, 72 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **100.0%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 75.0% | 100% | 38% | 100% | 42% | 100% | 67% | 75% | 100% | 100% | 50% |
| bm25 | 69.4% | 88% | 0% | 100% | 67% | 50% | 17% | 100% | 100% | 0% | 100% |

- Ingest: 4.87s for 102,660 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 164 facts / 9,532 chunks / 65 commits (9 derived by Datalog)
- Provenance completeness: 97.2% | retrieval latency p50≈8.55ms

## Bucket: 1M (1,000,631 est. tokens, 109 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **100.0%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 73.4% | 92% | 33% | 100% | 44% | 100% | 56% | 83% | 100% | 100% | 44% |
| bm25 | 68.8% | 75% | 8% | 100% | 67% | 50% | 11% | 100% | 100% | 0% | 100% |

- Ingest: 10.08s for 99,251 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 245 facts / 18,889 chunks / 93 commits (13 derived by Datalog)
- Provenance completeness: 97.2% | retrieval latency p50≈8.57ms

## Bucket: 10M (10,000,088 est. tokens, 214 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **100.0%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 69.2% | 83% | 25% | 100% | 53% | 100% | 33% | 83% | 100% | 100% | 28% |
| bm25 | 61.7% | 75% | 17% | 100% | 67% | 50% | 0% | 0% | 100% | 0% | 89% |

- Ingest: 102.88s for 97,204 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 583 facts / 189,859 chunks / 338 commits (26 derived by Datalog)
- Provenance completeness: 97.2% | retrieval latency p50≈9.11ms
