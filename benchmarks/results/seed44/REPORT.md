# Context-M — BEAM-Style Benchmark Results

## Bucket: 128K (128,629 est. tokens, 37 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **100.0%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 62.2% | 100% | 0% | 100% | 17% | 100% | 0% | 100% | 100% | 100% | 33% |
| bm25 | 67.6% | 75% | 0% | 100% | 67% | 50% | 0% | 100% | 100% | 0% | 100% |

- Ingest: 1.21s for 106,294 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 75 facts / 2,447 chunks / 27 commits (4 derived by Datalog)
- Provenance completeness: 100.0% | retrieval latency p50≈6.58ms

## Bucket: 500K (500,175 est. tokens, 72 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **97.2%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 67% |
| vector_only | 66.7% | 100% | 12% | 100% | 33% | 100% | 33% | 100% | 100% | 100% | 17% |
| bm25 | 69.4% | 75% | 0% | 100% | 67% | 50% | 33% | 100% | 100% | 0% | 100% |

- Ingest: 4.69s for 106,672 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 162 facts / 9,608 chunks / 59 commits (8 derived by Datalog)
- Provenance completeness: 100.0% | retrieval latency p50≈7.03ms

## Bucket: 1M (1,000,056 est. tokens, 109 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **97.2%** | 100% | 100% | 100% | 94% | 100% | 100% | 100% | 100% | 100% | 78% |
| vector_only | 68.8% | 100% | 33% | 100% | 28% | 100% | 33% | 100% | 100% | 100% | 22% |
| bm25 | 64.2% | 75% | 0% | 100% | 67% | 50% | 22% | 33% | 100% | 0% | 89% |

- Ingest: 9.82s for 101,817 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 244 facts / 19,122 chunks / 90 commits (12 derived by Datalog)
- Provenance completeness: 100.0% | retrieval latency p50≈7.38ms

## Bucket: 10M (10,000,065 est. tokens, 216 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **98.2%** | 100% | 100% | 100% | 92% | 100% | 100% | 100% | 100% | 100% | 94% |
| vector_only | 66.7% | 92% | 29% | 100% | 36% | 92% | 22% | 92% | 100% | 100% | 22% |
| bm25 | 58.8% | 75% | 8% | 100% | 67% | 50% | 0% | 0% | 97% | 0% | 67% |

- Ingest: 97.76s for 102,289 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 576 facts / 189,549 chunks / 349 commits (28 derived by Datalog)
- Provenance completeness: 100.0% | retrieval latency p50≈7.64ms
