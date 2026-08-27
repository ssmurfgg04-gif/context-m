# Benchmark Methodology

## Why a BEAM-style harness

The strategic brief targets **BEAM-10M** (arXiv:2510.27246, "Beyond a
Million Tokens: Benchmarking and Enhancing Long-Term Memory in LLMs").
BEAM's own pipeline is: auto-generate long, coherent, topically diverse
conversations (100K → 10M tokens), probe them with validated questions
across **ten memory abilities**, and score with nugget-based
evaluation. Our harness mirrors that methodology end-to-end so results
are comparable in spirit and honest in protocol.

**The ten abilities** are BEAM's own taxonomy (Table 1 of the paper):
Abstention, Contradiction Resolution, Event Ordering, Information
Extraction, Instruction Following, Knowledge Update, Multi-Hop
Reasoning, Preference Following, Summarization, Temporal Reasoning.

## Corpus generation (`context_m/bench/generator.py`)

- **Personas** carry ground-truth timelines: employment chains with
  explicit left/joined events, residence moves, preference flips,
  skills, family, manager→team→tech-stack chains (for multi-hop),
  projects (for summarization), dated life events (for ordering), and a
  standing instruction.
- Personas surface in **multi-session conversations** (sessions dated
  weeks apart) using varied natural phrasings — the extractor must
  genuinely parse them; the generator never writes ground truth into
  the store.
- **Distractor noise** fills buckets to target size: smalltalk turns
  plus long-form topical paragraphs with competing capitalized entities
  (Kafka Museum, Lake Baikal…), interleaved into persona sessions and
  appended as noise sessions. Buckets are sized by *measured* token
  estimates: 128K / 500K / 1M / 10M (±0.01%).
- Everything is seeded (default 42) — runs are bit-for-bit reproducible.

## Protocol

1. **Ingest** the corpus under the μ=0 protocol. The process-wide LLM
   call counter (`context_m.metrics`) is asserted to be zero; the
   report prints the count.
2. **Probe** with ability-targeted questions built from ground truth
   (identical question sets for all systems).
3. **Score with the deterministic nugget judge** — context
   sufficiency: a probe is answered iff the retrieved context contains
   the ground-truth nuggets an LLM reader would need (BEAM's nugget
   philosophy, minus the LLM: our judge parses fact lines and checks
   values, dates, ordering notes, supersession evidence, and set-F1).
   Abstention probes invert the check: any fabricated or unrelated
   answer-bearing fact fails.
4. **Baselines** run on the identical corpus and questions:
   - `bm25` — lexical RAG over raw message chunks (top-8), the
     "context stuffer" proxy.
   - `vector_only` — our own palace *without* the symbolic read path
     (fact-level neural RAG, source chunks returned). This isolates the
     neuro-symbolic delta: same vectors, same codecs, no planner.

## What is deliberately different from canonical BEAM

Canonical BEAM uses an LLM reader + LLM judge (gpt-5-class). This
harness ships a **deterministic judge** so the entire pipeline —
ingest, retrieval, judging — runs offline at $0 and is exactly
reproducible; `run_bucket(..., llm_judge=...)` accepts a pluggable
LLM judge for canonical-protocol replication. Second, our conversations
are synthetic-but-structured rather than model-generated dialogue;
this trades surface naturalness for exact ground truth and
deterministic scoring. Both differences are conservative: they make the
*system* work harder (no reader-LLM leniency) and the *scores* harder
to inflate.

## Fairness notes

- The extractor was developed against the pattern templates, so
  extraction accuracy on this corpus is an upper bound for arbitrary
  text; the μ=0 protocol means every extracted fact is auditable, and
  extraction misses show up honestly as lower scores (IE is the
  ability that exposes them).
- The baselines get the same chunk budget our context block effectively
  delivers; BM25 is a strong baseline on these corpora (keyword overlap
  with question phrasing) and still loses by 25-45 points overall.
- Timing was measured on the sandbox CPU (Python 3.12, numpy 2.1); no
  GPU, no parallelism, no Rust hot path — the plan's Rust port is an
  overlay, not a prerequisite.

## Reproducing

```bash
python -m context_m.bench.run --buckets 128k,500k,1m,10m
python -m context_m.bench.run --micro
```

Artifacts land in `benchmarks/results/` (per-bucket JSON with every
probe's question, score, judge reason, and retrieved context — fully
inspectable). `docs/BENCHMARKS.md` is generated from those JSONs by
`scripts/make_benchmarks_doc.py`.
