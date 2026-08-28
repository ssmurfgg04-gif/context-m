# LLM-judge evaluation

_backend: `gemini` — generated 2026-08-28 14:46 UTC_

### OOD judge cross-check

- items scored: **240/240**
- LLM judge mean: **0.2229** vs det judge mean: **0.3354**
- exact agreement: **82.1%**, within 0.5: **87.1%**
- judge model(s): `['gemini:gemini-3.5-flash-lite']`
- LLM judge mean 0.223 vs deterministic judge mean 0.335; exact agreement 82.1%. Two independent graders — the offline judge is not silently inflating scores (it grades higher here)
- protocol: BEAM-style context-sufficiency rubric replicated with the recorded judge model(s); canonical BEAM uses gpt-5 — numbers are NOT directly comparable across judge models.

### Real-GitHub track — μ=0 extractor vs LLM reference extractor

- threads: **5**, comments: **150**
- μ=0: **258 facts**, 8.81 ms/comment, $0.0 cost
- LLM reference: **173 facts**, 0.33 ms/comment, 89748 tokens, model `gemini:gemini-3.5-flash-lite`
- recall vs LLM reference: **0.052**
- precision vs LLM reference: **0.0581**

### Real-GitHub track — retrieval graded by the LLM judge

- questions: **17**
- overall: **0.2353**
- answerable: **0.0** | abstention: **1.0**
- judge model(s): `['gemini:gemini-3.5-flash-lite']`
