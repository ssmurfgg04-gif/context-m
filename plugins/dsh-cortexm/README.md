# dsh-cortexm

**DeepSeek Harness (Cordis) plugin — bi-temporal VSA memory for DSH agents.**

[![dsh-plugin](https://img.shields.io/badge/dsh-plugin-blue)](#)
[![cordis](https://img.shields.io/badge/cordis-storage+session-green)](#)
[![npm version](https://img.shields.io/badge/npm-0.1.0-orange)](#)

> **Status (2026-08-29):** scaffold. Manifest + storage/session API
> defined; JSON-RPC bridge to `cortexm serve` implemented; basic
> registration test passes. End-to-end with a real Python process
> validated by the Python test suite. Submit to
> `awesome-deepseek-harness` once the first stable release is on npm.

---

## What this is

`dsh-cortexm` is a native [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) plugin that exposes Context-M's
bi-temporal VSA memory + cognition engine + BLAKE3-chained provenance
as a DSH **storage + session** plugin.

| Capability | How DSH agents use it |
|---|---|
| **Storage** — `add`, `search`, `structural_query`, `consolidate`, `export_provenance`, `audit` | `ctx.storage.cortexm.add({ user_id, text })` |
| **Session** — `replay`, `fork`, `trajectory`, `inspect` | `ctx.session.cortexm.replay({ user_id, from_ts })` |

**Why this matters (DSH deep-dive, 2026-08-29):** DSH is the fastest-growing agent framework in H2 2026. The existing DSH memory plugins (`dsh-mnemon`, `dsh-engramory`, `dsh-memory-plugin`, `dsh-continual-evolve`) have **none** of:
- bi-temporal provenance
- VSA holographic retrieval
- a cognition engine
- BLAKE3-chained audit log

Context-M ships all four. `dsh-cortexm` is the premium memory plugin for DSH.

---

## Install

```bash
# 1. Install the Python backend (memory primitives live in Python)
pip install cortexm

# 2. Install the DSH plugin
dsh plugin add dsh-cortexm
# or directly from npm:
npm install dsh-cortexm
```

## Use

```js
// DSH agent preset
export default {
  plugins: ["cortexm"],
  // The agent gets memory through ctx.storage.cortexm.* and
  // ctx.session.cortexm.* — no MCP server process, no separate
  // REST API. Just `dsh plugin add dsh-cortexm`.
};

// In a tool handler:
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

### Reddit-driven features (2026-08-29 deep dive, ≥10 mentions each)
- **BM25 chunk-recall** (`CORTEXM_CHUNK_RECALL_USE_BM25=true` default) — Okapi BM25 (k1=1.5, b=0.75) for lexical scoring instead of Jaccard. Lifts the catastrophic `recall=0.052` on natural-language queries with rare terms (PR numbers, usernames, version strings).
- **`cortexm inspect` CLI + `contextm_inspect` MCP tool** — dump facts/chunks/audit for a scope as pretty JSON. The CLI-native answer to the "UI / dashboard / viewer / inspect" ask.
- **Session replay / fork / trajectory** — `ctx.session.cortexm.replay({ user_id, from_ts })`. The audit log already has every event; replay is just re-emitting them in order.

## Config (env vars or DSH plugin config)

| Env var | Default | Description |
|---|---|---|
| `CORTEXM_DB` | `:memory:` | SQLite path for the bi-temporal Trace |
| `CORTEXM_CODEC` | `int8` | VSA codec — `int8`/`binary`/`rabitq`/`pq` |
| `CORTEXM_FADE` | `exponential` | FadeMem forgetting curve |
| `CORTEXM_COGNITION` | `true` | HMS cognition engine on `consolidate` |
| `CORTEXM_PROVENANCE` | `true` | BLAKE3-chained audit + ZK-provenance export |
| `CORTEXM_CHUNK_RECALL_USE_BM25` | `true` | Okapi BM25 lexical scoring (≥10 Reddit mentions) |

## License

MIT — see `LICENSE` in the repository root.
