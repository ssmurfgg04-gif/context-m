# LLM-judge evaluation

_backend: `gemini` — generated 2026-08-27 18:14 UTC_

### OOD judge cross-check

- items scored: **237/240**
- LLM judge mean: **0.2215** vs det judge mean: **0.3354**
- exact agreement: **82.7%**, within 0.5: **87.8%**
- judge model(s): `['gemini:gemini-3.5-flash-lite']`
- LLM judge mean 0.222 vs deterministic judge mean 0.335; exact agreement 82.7%. Two independent graders — the offline judge is not silently inflating scores (it grades higher here)
- protocol: BEAM-style context-sufficiency rubric replicated with the recorded judge model(s); canonical BEAM uses gpt-5 — numbers are NOT directly comparable across judge models.

### Real-GitHub track — μ=0 extractor vs LLM reference extractor

- threads: **5**, comments: **150**
- μ=0: **16 facts**, 1.11 ms/comment, $0.0 cost
- LLM reference: **158 facts**, 2779.39 ms/comment, 89655 tokens, model `gemini:gemini-3.5-flash-lite`
- recall vs LLM reference: **0.0063**
- precision vs LLM reference: **0.0625**

### Real-GitHub track — retrieval graded by the LLM judge

- questions: **19**
- overall: **0.2632**
- answerable: **0.0667** | abstention: **1.0**
- judge model(s): `['gemini:gemini-3.5-flash-lite']`
