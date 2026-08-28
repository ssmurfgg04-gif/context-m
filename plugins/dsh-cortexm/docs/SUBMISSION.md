# awesome-deepseek-harness submission template

Submit `dsh-cortexm` to the curated list of community plugins for
DeepSeek Harness. The list lives at:

  https://github.com/deepseek-ai/awesome-deepseek-harness

## Submission entry (paste into awesome-deepseek-harness README)

```markdown
### dsh-cortexm

[![npm version](https://img.shields.io/badge/npm-0.1.0-orange)](https://www.npmjs.com/package/dsh-cortexm)
[![dsh-plugin](https://img.shields.io/badge/dsh-plugin-blue)](#)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Bi-temporal VSA memory + HMS cognition engine + BLAKE3-chained
provenance for DSH agents. Exposes Context-M as a storage +
session plugin via JSON-RPC over stdio to `cortexm serve`.

**Kind:** storage + session

**Why this matters:** existing DSH memory plugins
(`dsh-mnemon`, `dsh-engramory`, `dsh-memory-plugin`,
`dsh-continual-evolve`) have none of:
- bi-temporal provenance (every fact has `tx_from`/`tx_to`)
- VSA holographic retrieval (HRR superpositions, not raw text)
- a cognition engine (PatternScanner + AbstractionEngine +
  GapDetector + HypothesisEngine + AnalogyDetector)
- BLAKE3-chained audit log (cryptographically verifiable)

`dsh-cortexm` ships all four. It's the premium memory plugin
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

**Repo:** [ssmurfgg04-gif/context-m](https://github.com/ssmurfgg04-gif/context-m/tree/main/plugins/dsh-cortexm)
**Docs:** [README](https://github.com/ssmurfgg04-gif/context-m/blob/main/plugins/dsh-cortexm/README.md)
**License:** MIT
```

## Submission checklist

- [ ] Stable npm release (≥ 1.0.0) — currently scaffold (0.1.0)
- [ ] End-to-end test with real `cortexm serve` subprocess passing
- [ ] README in repo with install + use + architecture sections
- [ ] License file at repo root (already MIT)
- [ ] `dsh-plugin` topic tag on the npm package
- [ ] Submission PR to `awesome-deepseek-harness` with the entry above
- [ ] Cross-link from `dsh-market` (DSH Web UI marketplace) once the
      marketplace submission API is documented
- [ ] Twitter/Reddit announcement post in r/LocalLLaMA + r/LLMFrameworks
      (both subs have active "what memory library should I use?" threads)

## Distribution channels (per DSH deep-dive lessons)

| Channel | Status | Action |
|---|---|---|
| PyPI (`cortexm` package) | ✅ published 0.3.0 | none |
| npm (`dsh-cortexm` package) | scaffold ready | publish 1.0.0 after E2E test |
| MCP registry (`registry.modelcontextprotocol.io`) | submission JSON ready (`deploy/mcp-registry-submission.json`) | submit to the registry form |
| awesome-deepseek-harness | this template | submit PR after npm publish |
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
