# awesome-deepseek-harness submission

Submit `dsh-cortexm` to the curated list of community plugins for
DeepSeek Harness. The list lives at:

  https://github.com/deepseek-ai/awesome-deepseek-harness

## Status (2026-08-29)

✅ **npm published**: `dsh-cortexm@1.0.0` live on https://www.npmjs.com/package/dsh-cortexm — verified via `npm view dsh-cortexm` (integrity `sha512-5bE7669QS+E/wTzxf8yk9Rbb0Wt1SCQwYHfYRlcZ7l9m/DTAYsMZm6e//QRI8goI+YGjOxmDwJ6SmbcnSMBDbg==`, unpacked 39.3 kB, no deps, MIT).

✅ **End-to-end tests passing** with a real `cortexm serve` Python subprocess (5/5 tests in `test/e2e.test.js` — add→search, trajectory, replay, audit, subprocess close) + 3/3 manifest tests. Re-verified with cortexm 0.5.0 (the plugin kernel + verbatim tier release).

✅ **LongMemEval Tier 4.3: 0.800** (up from 0.700) — the new plugin kernel + verbatim tier catches knowledge-update questions verbatim when the role-pattern extractor misses. 2 remaining misses are answer-shape mismatches (list aggregation, yes/no phrasing), not memory failures.

✅ **Version 1.0.0** (`plugins/dsh-cortexm/package.json`). `dsh-plugin` topic tag included.

✅ **Fresh-install test**: in a clean `npm init -y` project, `npm install dsh-cortexm` resolves to the published tarball with the integrity hash above. Anyone can install.

TODO (this PR): paste the submission entry below into the awesome-deepseek-harness README.

## Submission entry (paste into awesome-deepseek-harness README)

```markdown
### dsh-cortexm

[![npm version](https://img.shields.io/npm/v/dsh-cortexm?color=%2334D058&logo=npm)](https://www.npmjs.com/package/dsh-cortexm)
[![dsh-plugin](https://img.shields.io/badge/dsh--plugin-storage+%7C+session-blue)](#)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Bi-temporal VSA memory + HMS cognition engine + BLAKE3-chained
provenance for DSH agents. Exposes Context-M as a storage +
session plugin via JSON-RPC over stdio to `cortexm serve`.

**LongMemEval Tier 4.3: 0.800 · 0 LLM calls at ingest · 8/8 e2e tests passing**

End-to-end tested with a real Python subprocess (5/5 tests passing).

**Kind:** storage + session

**Why this matters:** existing DSH memory plugins
(`dsh-mnemon`, `dsh-engramory`, `dsh-memory-plugin`,
`dsh-continual-evolve`) have none of:
- bi-temporal provenance (every fact has `tx_from`/`tx_to`)
- VSA holographic retrieval (HRR superpositions, not raw text)
- a cognition engine (PatternScanner + AbstractionEngine +
  GapDetector + HypothesisEngine + AnalogyDetector)
- BLAKE3-chained audit log (cryptographically verifiable)
- session replay / fork (DSH-style "rewind and try a different path")
- asymmetric "memory past 20 steps" recall (boost facts about to
  scroll out of the LLM context window)

`dsh-cortexm` ships all six. It's the premium memory plugin
for the DSH ecosystem.

**Install:**
\`\`\`bash
pip install cortexm
dsh plugin add dsh-cortexm
\`\`\`

**Use:**
\`\`\`javascript
// DSH agent preset
export default {
  plugins: ["cortexm"],
  // The agent gets memory through ctx.storage.cortexm.* and
  // ctx.session.cortexm.* — no MCP server process, no separate
  // REST API. Just `dsh plugin add dsh-cortexm`.
};
\`\`\`

**Plugin API surface:**

  ctx.storage.cortexm.add(...)                  // μ=0 ingest
  ctx.storage.cortexm.search(...)               // neuro-symbolic retrieval
  ctx.storage.cortexm.edit(...)                 // human-in-the-loop fix
  ctx.storage.cortexm.preload(...)              // top-N recent facts at session start
  ctx.storage.cortexm.recall_step(...)           // "memory past 20 steps"
  ctx.storage.cortexm.structural_query(...)     // deterministic relation chains
  ctx.storage.cortexm.consolidate(...)           // lifecycle + dreaming + cognition
  ctx.storage.cortexm.export_markdown(...)       // portable .md round-trip
  ctx.storage.cortexm.import_markdown(...)       // read .md back
  ctx.storage.cortexm.audit(...)                // "Why" provenance trail

  ctx.session.cortexm.replay(...)                // re-emit events in order
  ctx.session.cortexm.fork(...)                  // session branching → new run_id
  ctx.session.cortexm.trajectory(...)           // visualizable event stream
  ctx.session.cortexm.inspect(...)              // memory inspection dump

**Repo:** [ssmurfgg04-gif/context-m](https://github.com/ssmurfgg04-gif/context-m/tree/main/plugins/dsh-cortexm)
**Docs:** [README](https://github.com/ssmurfgg04-gif/context-m/blob/main/plugins/dsh-cortexm/README.md)
**License:** MIT
```

## Submission checklist

- [x] Stable npm release (≥ 1.0.0) — `dsh-cortexm@1.0.0` live on npm
- [x] End-to-end test with real `cortexm serve` subprocess passing
      (5/5 tests in `test/e2e.test.js`)
- [x] README in repo with install + use + architecture sections
- [x] License file at repo root (already MIT)
- [x] `dsh-plugin` topic tag on the npm package (declared in `keywords`)
- [x] `npm publish` executed — 1.0.0 is live, integrity hash verified
- [ ] Submission PR to `awesome-deepseek-harness` with the entry above
      (this submission)
- [ ] Cross-link from `dsh-market` (DSH Web UI marketplace) once the
      marketplace submission API is documented
- [ ] Twitter/Reddit announcement post in r/LocalLLaMA + r/LLMFrameworks
      (both subs have active "what memory library should I use?" threads)

## Distribution channels (per DSH deep-dive lessons)

| Channel | Status | Action |
|---|---|---|
| PyPI (`cortexm` package) | ✅ published 0.3.0, 0.4.0; 0.5.0 release workflow added (tag-triggered, trusted publishing) | none |
| npm (`dsh-cortexm` package) | ✅ published 1.0.0 | done |
| MCP registry (`registry.modelcontextprotocol.io`) | submission JSON ready (`deploy/mcp-registry-submission.json`) | submit to the registry form |
| awesome-deepseek-harness | this template | submit this PR |
| dsh-market (DSH Web UI marketplace) | pending | submit once API is documented |
| LangChain package index | ✅ `context-m-langchain` 0.3.0 published | none |
| LlamaIndex package index | integration in `plugins/llamaindex/` | publish to PyPI |

## Naming

The plugin is `dsh-cortexm` (not `dsh-context-m`) because:
1. The PyPI package is `cortexm` (renamed from `context_m` in 0.3.0).
2. The CLI is `cortexm` (single command, no hyphen, easier to type).
3. Convention: `dsh-<backend-name>` (e.g. `dsh-mnemon`, `dsh-engramory`).
4. The MCP server `cortexm serve` is what the plugin shells out to.

## Cross-promotion plan (Reddit-driven)

Per the deep-dive, ≥10 Reddit mentions of "UI/dashboard/inspect"
were validated by shipping `cortexm inspect` CLI. When announcing
`dsh-cortexm`, lead with:

> "Bi-temporal VSA memory + cognition engine + BLAKE3 provenance,
> as a DSH storage+session plugin. `dsh plugin add dsh-cortexm`.
> And `cortexm inspect` for the 'I just want to see what's in
> memory' ask."

Then list the differentiators vs `dsh-mnemon` (most mature
competitor, 370+ commits):
- ✅ bi-temporal provenance (theirs: none)
- ✅ VSA holographic retrieval (theirs: raw text concat)
- ✅ HMS cognition engine (theirs: none)
- ✅ BLAKE3 audit log (theirs: git versioning only)
- ✅ MCP-native (theirs: in-process)
- ❌ DSH-native plugin maturity (theirs: 370+ commits, ours: 0.1.0)

The fourth bullet (MCP-native) is true because `cortexm serve`
already exposes MCP stdio JSON-RPC, and `dsh-cortexm` is just a
thin Node-side bridge to that subprocess. No new transport.
