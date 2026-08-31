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

| | cortexm v0.6.4 | MemPalace (honest E2E) |
|---|---|---|
| **canonical LongMemEval (500-Q full corpus)** | **97.4% (487/500)** | ~96.6% (retrieval-only, no QA) |
| single_session | **100.0%** | — |
| knowledge_update | **100.0%** | — |
| multi_session | 94.74% | — |
| temporal_reasoning | 95.49% | — |
| LLM calls (ingest + retrieval + judge) | 0 | 0 |
| monthly cost | $0 | $0 |
| determinism | byte-exact across 3× runs | byte-exact |
| owns your data | ✓ single `.db` file | ✓ |

**Full 500-question results** (v0.6.2 baseline; v0.6.4 re-run lands the experimental graph-recall + coherence modules below):

| Subtask | Score | Notes |
|---|---|---|
| **Overall** | **0.974 (487/500)** | Full corpus, not a proxy sample |
| single_session | **1.000** | Perfect retrieval across all sessions |
| knowledge_update | **1.000** | Supersession edges working correctly |
| temporal_reasoning | 0.9549 | 6 failures on long-distance relative refs (>2 weeks) |
| multi_session | 0.9474 | 7 failures; 4 retrieval misses, 2–3 arithmetic aggregation gaps |

| Strategy | Score |
|---|---|
| holiday_date, paren_abbreviation, list, sum_or_diff | **1.000** |
| nugget | 0.9691 |
| bool | 0.8571 |

**Baseline beaten:** v0.5.5 baseline was 0.948; this is a **+2.6 pp** improvement on the full 500-question corpus.

**Known remaining gaps (diagnosed, not guessed):**
- **Temporal anchoring** — degrades on multi-week relative references ("four weeks ago", "10 days ago"). These 6 failures connect to the `temporal_chain_notes` / supersession-history mechanism in `reader.py`. v0.6.4's `cortexm/experimental/coherence.py` adds a deterministic temporal-coherence rerank signal aimed at exactly these.
- **Arithmetic aggregation** — the generalized `sum_or_diff` judge (v0.6.2) fixes the 2–3 real computation gaps. The remaining multi_session failures are **retrieval misses** (wrong session pulled: poetry instead of podcasts, marketing facts instead of video views), not judge failures. v0.6.4 wires the previously-dead `percentage`/`numeric_agg` judges and adds `cortexm/experimental/graph_recall.py` (entity-adjacency 2-hop walks) aimed at the wrong-session misses.
- **BOOL strategy** at 85.7% is the weakest category — needs sign-of-evidence refinement for edge cases.

Run the full 500-Q benchmark via GitHub Actions: `.github/workflows/longmemeval.yml` (20 shards, ~30s/q with per-shard DB caching).

### Known boundaries (the short list)

> Full detail: [`docs/FAILURE_MODES.md`](docs/FAILURE_MODES.md) — every failure tied to a public benchmark question.

1. **The extractor is a 61-pattern lookup, not a language model.** Phrasings outside the pattern library are silently dropped at ingest (e.g. "Anna has a cat named Whiskers") — they remain retrievable via verbatim/BM25 chunk recall, but never become structured facts. This is the price of μ=0: no generativity, no fabrication, no drift.
2. **ZK proofs are trusted-prover attestations.** The v0.6.4 backend (Pedersen + Sigma protocols on secp256k1) is sound at the commitment layer — challenges are bound to announcements, both OR-proof branches verify, H has no known discrete log, thresholds are enforced — but the linkage between committed values and store rows is established at prove-time by the prover. Verify the integration layer before trusting it against a malicious host.
3. **Set membership reveals the leaf index.** The value stays hidden (random-blinding Pedersen + equality proof); the position in the set does not. Position-hiding needs a ZK-friendly Merkle construction — documented future work.
4. **No cross-user inference, ever.** Every fact is scoped by `user_id`; the scope sandbox turns empty scopes into empty results (not unrestricted fallbacks). This is a feature, and it also means no "insight across users" stories.
5. **Compression tiers are documented, not default.** int8/binary quantization trade recall for space (see `docs/COMPRESSION.md`); the default build keeps full-precision embeddings because the benchmark headroom doesn't justify the loss yet.
6. **Judge coverage is rule-based.** The deterministic judge answers via strategy dispatch (bool/list/nugget/sum_or_diff/percentage/numeric_agg/holiday/paren). Questions outside those strategies score 0 even when retrieval succeeded — the failure is honest, the number is real.

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
- [`tests/`](tests/) — 698 tests: fabric + enterprise + PPR + concurrency + sandbox + enrichment + WAL crash-recovery + migration + CRDT federation + Rust parity + ZK soundness/forgery + public-API smoke
- [`cortexm/experimental/`](cortexm/experimental/) — deterministic research borrows (graph recall, coherence) — μ=0 or it doesn't ship
- [`leaderboard/`](leaderboard/) — self-hosted benchmark site (rebuild: `python leaderboard/build.py`; open `leaderboard/index.html`)
- [`AGENTS.md`](AGENTS.md) — how AI coding agents should interact with this repo (2026 standard)
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — contribution guide

### License

Apache 2.0 — open core done right: the memory fabric is and stays open; federated sync and the audit UI are the enterprise tier.
