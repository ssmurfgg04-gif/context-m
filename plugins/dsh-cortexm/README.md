<div align="center">

# dsh-cortexm

**Memory for DeepSeek Harness agents.**
Remembers what you tell it — forever, for free, on your machine.
You can check exactly what it remembers and why.
And it works the same way every single time.

[![npm version](https://img.shields.io/npm/v/dsh-cortexm?color=%2334D058&label=npm%20%7Cdsh-cortexm&logo=npm)](https://www.npmjs.com/package/dsh-cortexm)
[![dsh-plugin](https://img.shields.io/badge/dsh--plugin-storage+%7C+session-blue)](#)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![cortexm](https://img.shields.io/pypi/v/cortexm?color=%2334D058&label=pypi%20%7Ccortexm%20backend)](https://pypi.org/project/cortexm/)

**LongMemEval Tier 4.3: 0.800** · **0 LLM calls at ingest** · **8/8 e2e tests passing**

</div>

---

> [!WARNING]
> Install only from `npm: dsh-cortexm` or the GitHub source. Unofficial
> mirrors / re-uploads are not reviewed and may inject malicious storage
> or session adapters. The maintainer is `cortexm` on npm; the verified
> shasum is published in the npm manifest.

## What it is

`dsh-cortexm` is a native [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
plugin that exposes Context-M's bi-temporal VSA memory + cognition
engine + BLAKE3-chained provenance as a DSH **storage + session**
plugin.

```bash
# 1. Python backend (memory primitives live in Python)
pip install cortexm

# 2. DSH plugin
dsh plugin add dsh-cortexm
# or, equivalently:
npm install dsh-cortexm
```

```js
// DSH agent preset
export default {
  plugins: ["cortexm"],
};

// In a tool handler
const ctx = dsh.useContext();
await ctx.storage.cortexm.add({
  user_id: "alice",
  agent_id: "research-bot",
  run_id: "session-1",
  text: "Alice works at Acme Corp. She prefers email.",
});

const hits = await ctx.storage.cortexm.search({
  user_id: "alice",
  query: "Where does Alice work?",
});
```

## Why this matters

DSH is the fastest-growing agent framework of 2026 (`deepseek-ai/deepseek-harness`, 200k+ stars).
The existing DSH memory plugins (`dsh-mnemon`, `dsh-engramory`,
`dsh-memory-plugin`, `dsh-continual-evolve`) ship **none** of:

- bi-temporal provenance (every fact has `tx_from` / `tx_to`)
- VSA holographic retrieval (HRR superpositions, not raw text concat)
- a cognition engine (PatternScanner + AbstractionEngine +
  GapDetector + HypothesisEngine + AnalogyDetector)
- BLAKE3-chained audit log (cryptographically verifiable)
- session replay / fork (DSH-style "rewind and try a different path")
- asymmetric "memory past 20 steps" recall (boost facts about to
  scroll out of the LLM context window)

`dsh-cortexm` ships all six. **It is the premium memory plugin for
the DSH ecosystem.**

## Honesty-coded benchmark

| Benchmark | Number | Methodology |
|---|---|---|
| **LongMemEval Tier 4.3** | **0.800 overall** (single_hop 1.0 / knowledge_update 1.0 / multi_session 0.5 / temporal_reasoning 0.5) | 10-question synthetic subset, deterministic nugget judge, μ=0 ingest asserted, 2 misses are answer-shape mismatches (list aggregation, yes/no phrasing) — not memory failures. Reproduce: `python scripts/longmemeval_judge.py` |
| **μ=0 protocol** | 0 LLM calls at ingest | Enforced by `cortexm.LLM_CALLS` counter; every retrieval returns its full BLAKE3 provenance chain. |
| **End-to-end tests** | 8/8 passing | `node --test test/*.test.js` — real Python subprocess, add→search / trajectory / replay / audit / subprocess close. |

The LongMemEval number is **post-plugin-kernel (v0.5.0)** — the
previous 0.700 came from the structured tier alone; the new verbatim
tier (FTS5 + int8 dense, MemPalace-style) catches knowledge-update
questions verbatim when the role-pattern extractor misses
("I'm now working at OpenAI"). Fusion then merges both tiers at
μ=0 cost. The 2 remaining misses are not memory failures — they
are answer-shape mismatches.

## Architecture

```
DSH agent
  ↓ ctx.storage.cortexm.* / ctx.session.cortexm.*
dsh-cortexm plugin (Node, zero runtime deps)
  ↓ JSON-RPC over stdio
`cortexm serve` subprocess (Python)
  ↓
Trace (bi-temporal SQLite) + VSA Palace (HRR holograms) + HMS Cognition + BLAKE3 audit
```

### Cordis spatiotemporal composability

- `ctx.effect(register_fn, cleanup_fn)` — on plugin unload, the
  subprocess is closed, the stdio pipe is torn down, and **no orphan
  listeners** remain ("no orphan listener, no open connection and no
  ghost command left behind" — Cordis paper §3.4).
- Future-work: register `tools/pre-execute` for MINJA pattern scan +
  MIND diversity check on retrieved context, `tools/post-execute`
  for PII redaction on tool results (Reddit deep-dive: security as
  pipeline middleware is a ≥10-mention ask).

## Plugin API surface

```text
ctx.storage.cortexm.add(...)                  // μ=0 ingest
ctx.storage.cortexm.search(...)                // neuro-symbolic retrieval
ctx.storage.cortexm.edit(...)                  // human-in-the-loop fix
ctx.storage.cortexm.preload(...)               // top-N recent facts at session start
ctx.storage.cortexm.recall_step(...)            // "memory past 20 steps"
ctx.storage.cortexm.structural_query(...)      // deterministic relation chains
ctx.storage.cortexm.consolidate(...)            // lifecycle + dreaming + cognition
ctx.storage.cortexm.export_markdown(...)       // portable .md round-trip
ctx.storage.cortexm.import_markdown(...)       // read .md back
ctx.storage.cortexm.audit(...)                 // "Why" provenance trail

ctx.session.cortexm.replay(...)               // re-emit events in order
ctx.session.cortexm.fork(...)                 // session branching → new run_id
ctx.session.cortexm.trajectory(...)           // visualizable event stream
ctx.session.cortexm.inspect(...)              // memory inspection dump
```

## Config (env vars or DSH plugin config)

| Env var | Default | Description |
|---|---|---|
| `CORTEXM_DB` | `:memory:` | SQLite path for the bi-temporal Trace |
| `CORTEXM_CODEC` | `int8` | VSA codec — `int8`/`binary`/`rabitq`/`pq` |
| `CORTEXM_FADE` | `exponential` | FadeMem forgetting curve |
| `CORTEXM_COGNITION` | `true` | HMS cognition engine on `consolidate` |
| `CORTEXM_PROVENANCE` | `true` | BLAKE3-chained audit + ZK-provenance export |
| `CORTEXM_CHUNK_RECALL_USE_BM25` | `true` | Okapi BM25 lexical scoring (≥10 Reddit mentions) |

## Reproducibility

```bash
# LongMemEval Tier 4.3 — produces benchmarks/results/longmemeval_v0.5.0.json
python scripts/longmemeval_judge.py

# E2E tests (real Python subprocess, real JSON-RPC over stdio)
cd plugins/dsh-cortexm && npm test
```

All scripts are pure-Python stdlib; the only optional install is
`pip install cortexm[blake3]` for the BLAKE3 wheel (BLAKE2b-256
fallback with a loud warning is used if absent).

## License

MIT — see `LICENSE` in the repository root.
