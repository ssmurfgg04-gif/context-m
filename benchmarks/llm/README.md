# LLM evaluation harness — canonical BEAM-style judge

## What runs here

| Script | Role |
|---|---|
| `common.mjs` | Backend-agnostic plumbing: `LLM_BACKEND=zai\|gemini`, SHA-256 keyed cache (backend+model in the key — results never mix), bounded concurrency, retry/backoff, strict-JSON extraction |
| `render_ood.mjs` | OOD corpus rendering (personas re-phrased in 6 styles) |
| `extract_facts.mjs` | LLM reference extractor (the yardstick for μ=0 recall) |
| `qa_generate.mjs` | QA generation over real threads |
| `judge_llm.mjs` | **The canonical judge** — BEAM context-sufficiency rubric (arXiv:2510.27246 protocol replication); resumable per item, never re-bills |

## Backends

```bash
# GLM via the z-ai gateway (default)
LLM_BACKEND=zai node benchmarks/llm/judge_llm.mjs <items> <out>

# Gemini (any supported region)
LLM_BACKEND=gemini GEMINI_API_KEY=... LLM_MODEL=gemini-3.5-flash-lite \
  node benchmarks/llm/judge_llm.mjs <items> <out>
```

Every result row records `judge_model` and token usage; the aggregation
(`benchmarks/aggregate_judge.py`) records the judge identity in the
crosscheck JSON. **Canonical BEAM used gpt-5 — numbers are NOT directly
comparable across judge models**, and every artifact says so.

## Known limitation: region blocking

The Gemini Generative Language API refuses requests from unsupported
regions (HTTP 400 `FAILED_PRECONDITION "User location is not
supported"`). This sandbox egresses from Hong Kong — blocked. The
backend detects this and fails fast with an actionable message instead
of burning retries.

Two ways to run it for real:

1. **From any supported region** (US/EU/…): the commands above,
   unchanged.

2. **GitHub Actions** (runners are US-based): add the repo secret
   `GEMINI_API_KEY` (Settings → Secrets and variables → Actions), then
   run the `llm-eval` workflow (Actions tab → *llm-eval* → *Run
   workflow*). It judges the exact tracked contexts
   (`benchmarks/ood/judge_items.jsonl`, 240 items with deterministic
   scores attached), runs the real-GitHub track, aggregates results,
   commits them to the `bench/llm-eval` branch and uploads an artifact.
   The judge is resumable — killed runs keep every finished item.

## Current state of the numbers

- Deterministic judge: complete, all styles (`results/ood/summary.json`).
- LLM judge cross-check: 58/240 items scored with `glm-4-plus` before
  the gateway quota wall (`results/ood/llm_judge_crosscheck.json` —
  agreement 75.9%, LLM judge grades LOWER than the deterministic one).
- Gemini pass: harness ready, one command from numbers; blocked from
  this sandbox by region, runnable via the workflow above.
