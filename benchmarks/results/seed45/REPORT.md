# Context-M — BEAM-Style Benchmark Results

## Bucket: 128K (128,007 est. tokens, 35 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **91.4%** | 100% | 100% | 100% | 83% | 100% | 100% | 100% | 100% | 0% | 67% |
| vector_only | 65.7% | 100% | 0% | 100% | 50% | 100% | 33% | 100% | 100% | 100% | 0% |
| bm25 | 65.7% | 75% | 0% | 100% | 67% | 50% | 33% | 100% | 100% | 0% | 67% |

- Ingest: 1.17s for 108,988 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 75 facts / 2,434 chunks / 29 commits (5 derived by Datalog)
- Provenance completeness: 100.0% | retrieval latency p50≈6.42ms

## Bucket: 500K (500,093 est. tokens, 72 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **98.6%** | 100% | 100% | 100% | 92% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 64.6% | 88% | 0% | 100% | 50% | 100% | 17% | 88% | 100% | 100% | 17% |
| bm25 | 69.4% | 75% | 12% | 100% | 67% | 50% | 17% | 100% | 100% | 0% | 100% |

- Ingest: 4.59s for 108,894 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 163 facts / 9,552 chunks / 61 commits (10 derived by Datalog)
- Provenance completeness: 100.0% | retrieval latency p50≈7.61ms

## Bucket: 1M (1,000,010 est. tokens, 109 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **99.1%** | 100% | 100% | 100% | 94% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 70.2% | 100% | 17% | 100% | 50% | 100% | 33% | 92% | 100% | 100% | 22% |
| bm25 | 65.1% | 75% | 0% | 100% | 67% | 50% | 33% | 33% | 100% | 0% | 89% |

- Ingest: 9.25s for 108,118 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 244 facts / 18,994 chunks / 94 commits (14 derived by Datalog)
- Provenance completeness: 100.0% | retrieval latency p50≈6.85ms

## Bucket: 10M (10,000,060 est. tokens, 216 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **99.1%** | 100% | 100% | 100% | 94% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 66.0% | 92% | 21% | 100% | 44% | 100% | 17% | 71% | 100% | 100% | 22% |
| bm25 | 57.9% | 75% | 17% | 100% | 67% | 50% | 0% | 0% | 93% | 0% | 50% |

- Ingest: 98.04s for 101,995 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 602 facts / 189,445 chunks / 346 commits (29 derived by Datalog)
- Provenance completeness: 100.0% | retrieval latency p50≈7.85ms
