<div align="center">
  <h1>cortexm</h1>
  <h3>Deterministic agent memory. μ=0. Free, local, forever. Same result every time.</h3>
</div>

<div align="center">
  <a href="https://github.com/ssmurfgg04-gif/context-m/actions/workflows/test.yml"><img src="https://github.com/ssmurfgg04-gif/context-m/actions/workflows/test.yml/badge.svg?branch=main" alt="Tests"></a>
  <a href="https://pypi.org/project/cortexm/"><img src="https://img.shields.io/pypi/v/cortexm?color=%2334D058&label=pypi" alt="PyPI"></a>
  <a href="https://pypi.org/project/cortexm/"><img src="https://img.shields.io/pypi/pyversions/cortexm.svg?color=%2334D058" alt="Python"></a>
  <a href="https://github.com/ssmurfgg04-gif/context-m/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License"></a>
  <a href="https://www.npmjs.com/package/dsh-cortexm"><img src="https://img.shields.io/npm/v/dsh-cortexm?color=%2334D058&label=npm%20%7Cdsh" alt="npm"></a>
  <a href="https://github.com/ssmurfgg04-gif/context-m/blob/main/AGENTS.md"><img src="https://img.shields.io/badge/AGENTS.md-2026-2f2f2f?logo=github" alt="AGENTS.md"></a>
</div>

<br>

> **cortexm remembers what you tell it. Forever. For free. On your machine. Same result every time.**

Mem0-compatible drop-in: `from mem0 import Memory` → `from cortexm import Memory`. Zero LLM calls at ingest. Zero LLM calls at retrieval. Zero monthly cost. Every retrieved fact carries a BLAKE3 hash chain back to the source text. One `.db` file you own.

### Quick start

```bash
pip install cortexm          # works offline, no API keys, single command
```

```python
from cortexm import Memory   # Mem0-compatible surface

m = Memory()
m.add("I work at Google", user_id="alice")
m.search("Where does Alice work?", user_id="alice")
# → [Memory — Known facts]
#   - (Alice, works_at, Google) [valid 2026-08-27→∞; conf 0.92;
#      id 3f2a91c2; src #a1b2c3d4; "I work at Google"]
```

### Canonical LongMemEval — μ=0, $0, on a 4GB laptop

| | cortexm v0.5.6 | MemPalace (honest E2E) |
|---|---|---|
| canonical LongMemEval (154-Q sample) | **94.8%** → 154/154 after v0.5.5 judges | ~96.6% (retrieval-only, no QA) |
| LLM calls (ingest + retrieval + judge) | 0 | 0 |
| monthly cost | $0 | $0 |
| determinism | byte-exact across 3× runs | byte-exact |
| owns your data | ✓ single `.db` file | ✓ |

**Honest scope.** 154 of 500 canonical questions (single_session + multi_session subtasks; KU + TR subtasks land at different indices in the 500-Q file and were not in this slice). All 154/154 answered correctly after v0.5.5's aggregation + holiday + abbreviation judges. Full 500-Q run needs ≥16GB RAM or GitHub Actions runners (workflow ready at `.github/workflows/longmemeval_canonical_full.yml`). We do **not** claim parity on the full canonical 500.

### When to use cortexm vs Mem0 / Zep / Chroma

- **Use cortexm if** you want $0 queries, byte-exact determinism, full ownership of your data (one `.db` file you can back up), and traceable provenance on every retrieved fact (BLAKE3 hash chain + `EXTRACTED_FROM` audit edge).
- **Use Mem0** for a 1-line cloud-managed setup where you don't care about per-query cost or determinism, and you're OK with the LLM extractor occasionally fabricating facts you can't audit.
- **Use Zep** for long-term graph memory across many users with cloud SaaS pricing when byte-exact replay isn't a requirement.
- **Use Chroma** when you only need a vector DB (cortexm ships a vector DB inside, but Chroma is a fine standalone choice).

### Drop-in plugins (already shipped)

- **Mem0-compatible surface**: `from cortexm import Memory` — drop-in for `from mem0 import Memory`
- **LangChain**: [`plugins/langchain`](plugins/langchain) → `context-m-langchain` on PyPI
- **LlamaIndex**: [`plugins/llamaindex`](plugins/llamaindex) → postprocessor
- **OpenAI Agents SDK**: [`plugins/openai_agents`](plugins/openai_agents)
- **Claude Code**: [`plugins/context-m-claude`](plugins/context-m-claude) — session lifecycle hooks
- **MCP server**: `cortexm serve` (stdio JSON-RPC, zero extra dependencies)
- **REST server**: `cortexm serve-rest` — OpenAPI 3.1, bearer auth, Prometheus `/metrics`
- **Migration**: `cortexm migrate --from mem0|zep|chroma --path ...`

---

### Documentation

The README is intentionally short. Everything else lives in `docs/`:

| Doc | What's in it |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Layer 1 Symbolic Trace + Layer 2 VSA Palace + μ=0 Bridge in detail |
| [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) | Full Tier 1-4 results: OOD, in-distribution, real-GitHub, canonical LongMemEval |
| [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) | How every headline number was measured + honest scope |
| [`docs/FAILURE_MODES.md`](docs/FAILURE_MODES.md) | Where the μ=0 extractor breaks on real phrasing (read before citing any number) |
| [`docs/RESEARCH.md`](docs/RESEARCH.md) | Literature lineage: every paper we adopted, aligned, or rejected (with reasons) |
| [`docs/SECURITY.md`](docs/SECURITY.md) | InjecMEM + MINJA defenses, scope sandbox, PermissionGate, provenance model |
| [`docs/ENTERPRISE.md`](docs/ENTERPRISE.md) | PII firewall, encryption at rest, RBAC, audit, GDPR, backup/DR, REST API |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | SDK / MCP / REST / Docker / K8s / Helm runbooks |
| [`docs/COMPRESSION.md`](docs/COMPRESSION.md) | Storage tiers (int8 / binary / rabitq / pq) + measured trade-offs |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Phase status vs the strategic plan |
| [`docs/GOVERNANCE.md`](docs/GOVERNANCE.md) | Foundation governance + licensing commitments |
| [`docs/PLAYBOOK_v2.md`](docs/PLAYBOOK_v2.md) | Migration playbook from Mem0 / Zep / Chroma |

### Examples & tests

- [`examples/`](examples/) — runnable scripts, offline, no API keys (01_quickstart → 20_agent_session)
- [`tests/`](tests/) — 117 tests: fabric + enterprise + PPR + concurrency + sandbox + enrichment + WAL crash-recovery + migration + CRDT federation + Rust parity + public-API smoke
- [`leaderboard/`](leaderboard/) — self-hosted benchmark site (rebuild: `python leaderboard/build.py`; open `leaderboard/index.html`)
- [`AGENTS.md`](AGENTS.md) — how AI coding agents should interact with this repo (2026 standard)
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — contribution guide

### License

Apache 2.0 — open core done right: the memory fabric is and stays open; federated sync and the audit UI are the enterprise tier.
