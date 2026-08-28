# Context-M — BEAM-Style Benchmark Results

## Bucket: 128K (128,005 est. tokens, 35 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **100.0%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 68.6% | 100% | 0% | 100% | 50% | 100% | 33% | 100% | 100% | 100% | 33% |
| bm25 | 68.6% | 75% | 0% | 100% | 67% | 50% | 33% | 100% | 100% | 0% | 100% |

- Ingest: 1.24s for 102,944 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 76 facts / 2,434 chunks / 29 commits (5 derived by Datalog)
- Provenance completeness: 97.1% | retrieval latency p50≈7.66ms

## Bucket: 500K (500,085 est. tokens, 72 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **100.0%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 64.6% | 88% | 0% | 100% | 50% | 100% | 17% | 88% | 100% | 100% | 17% |
| bm25 | 69.4% | 75% | 12% | 100% | 67% | 50% | 17% | 100% | 100% | 0% | 100% |

- Ingest: 4.76s for 105,036 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 163 facts / 9,552 chunks / 61 commits (10 derived by Datalog)
- Provenance completeness: 97.2% | retrieval latency p50≈7.91ms

## Bucket: 1M (1,000,010 est. tokens, 109 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **100.0%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 70.2% | 100% | 17% | 100% | 50% | 100% | 33% | 92% | 100% | 100% | 22% |
| bm25 | 67.9% | 75% | 17% | 100% | 67% | 50% | 33% | 33% | 100% | 0% | 100% |

- Ingest: 9.81s for 101,933 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 244 facts / 18,995 chunks / 94 commits (14 derived by Datalog)
- Provenance completeness: 97.2% | retrieval latency p50≈8.42ms

## Bucket: 10M (10,000,000 est. tokens, 216 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **100.0%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 66.0% | 92% | 21% | 100% | 44% | 100% | 17% | 71% | 100% | 100% | 22% |
| bm25 | 60.2% | 75% | 17% | 100% | 67% | 50% | 0% | 0% | 93% | 0% | 78% |

- Ingest: 103.14s for 96,951 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 600 facts / 189,445 chunks / 346 commits (27 derived by Datalog)
- Provenance completeness: 97.2% | retrieval latency p50≈9.18ms
