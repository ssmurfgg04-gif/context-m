# Context-M — BEAM-Style Benchmark Results

## Bucket: 128K (128,099 est. tokens, 35 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **100.0%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 65.7% | 100% | 0% | 100% | 50% | 100% | 33% | 100% | 100% | 100% | 0% |
| bm25 | 71.4% | 75% | 50% | 100% | 67% | 50% | 0% | 100% | 100% | 0% | 100% |

- Ingest: 1.24s for 103,454 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 79 facts / 2,507 chunks / 33 commits (5 derived by Datalog)
- Provenance completeness: 97.1% | retrieval latency p50≈7.6ms

## Bucket: 500K (500,002 est. tokens, 72 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **100.0%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 63.9% | 88% | 12% | 100% | 33% | 100% | 0% | 100% | 100% | 100% | 33% |
| bm25 | 72.2% | 75% | 50% | 100% | 67% | 50% | 0% | 100% | 100% | 0% | 100% |

- Ingest: 4.89s for 102,225 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 174 facts / 9,523 chunks / 65 commits (9 derived by Datalog)
- Provenance completeness: 97.2% | retrieval latency p50≈8.24ms

## Bucket: 1M (1,001,133 est. tokens, 107 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **100.0%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 67.3% | 100% | 25% | 100% | 33% | 100% | 22% | 100% | 100% | 100% | 22% |
| bm25 | 71.0% | 75% | 33% | 100% | 67% | 50% | 11% | 100% | 100% | 0% | 100% |

- Ingest: 9.9s for 101,151 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 251 facts / 19,136 chunks / 87 commits (13 derived by Datalog)
- Provenance completeness: 96.3% | retrieval latency p50≈8.42ms

## Bucket: 10M (10,000,078 est. tokens, 212 questions)

| System | Overall | AB | CR | EO | IE | IF | KU | MH | PF | SZ | TR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| context_m | **100.0%** | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| vector_only | 63.4% | 92% | 21% | 100% | 33% | 100% | 17% | 71% | 100% | 100% | 22% |
| bm25 | 62.3% | 75% | 25% | 100% | 67% | 50% | 0% | 0% | 96% | 0% | 94% |

- Ingest: 105.19s for 95,065 tokens/s (μ=0: verified, 0 LLM calls)
- Memory: 594 facts / 188,936 chunks / 319 commits (26 derived by Datalog)
- Provenance completeness: 97.2% | retrieval latency p50≈9.65ms
