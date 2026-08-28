# Context-M — BEAM-Style Benchmark Results

## Bucket: 128K (128,627 est. tokens, 37 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **100.0%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 62.2% | 100% | 0% | 100% | 17% | 100% | 0% | 100% | 100% | 100% | 33% |
| bm25 | 67.6% | 75% | 0% | 100% | 67% | 50% | 0% | 100% | 100% | 0% | 100% |

- Ingest: 1.29s for 99,887 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 75 facts / 2,447 chunks / 27 commits (4 derived by Datalog)
- Provenance completeness: 97.3% | retrieval latency p50≈7.54ms

## Bucket: 500K (500,167 est. tokens, 72 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **100.0%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 66.7% | 100% | 12% | 100% | 33% | 100% | 33% | 100% | 100% | 100% | 17% |
| bm25 | 69.4% | 75% | 0% | 100% | 67% | 50% | 33% | 100% | 100% | 0% | 100% |

- Ingest: 4.89s for 102,246 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 162 facts / 9,608 chunks / 59 commits (8 derived by Datalog)
- Provenance completeness: 97.2% | retrieval latency p50≈7.98ms

## Bucket: 1M (1,000,044 est. tokens, 109 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **100.0%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 69.7% | 100% | 33% | 100% | 28% | 100% | 33% | 100% | 100% | 100% | 33% |
| bm25 | 65.1% | 75% | 0% | 100% | 67% | 50% | 22% | 33% | 100% | 0% | 100% |

- Ingest: 9.8s for 102,059 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 245 facts / 19,122 chunks / 90 commits (12 derived by Datalog)
- Provenance completeness: 97.2% | retrieval latency p50≈8.29ms

## Bucket: 10M (10,000,005 est. tokens, 216 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **100.0%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 66.7% | 92% | 29% | 100% | 36% | 92% | 22% | 92% | 100% | 100% | 22% |
| bm25 | 61.1% | 75% | 4% | 100% | 67% | 50% | 0% | 0% | 97% | 0% | 100% |

- Ingest: 102.27s for 97,778 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 573 facts / 189,549 chunks / 349 commits (25 derived by Datalog)
- Provenance completeness: 97.2% | retrieval latency p50≈9.29ms
