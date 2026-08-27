# Context-M — The Universal Neuro-Symbolic Memory Fabric

> **Mem0 gives your agent a notebook. Context-M gives your agent a brain.**

A memory substrate for AI agents that combines a **bi-temporal symbolic
Trace** (hippocampus) with a **VSA Memory Palace** (neocortex), bound by
a **μ=0 deterministic bridge** — zero LLM calls at ingest, cryptographic
provenance on every retrieval, edge-first deployment at 96 bytes per
memory.

```
pip install cortexm          # works offline, no API keys, single command
```

```python
from cortexm import Memory   # Mem0-compatible surface

m = Memory()
m.add("I work at Google", user_id="alice")
m.search("Where does Alice work?", user_id="alice")
# → [Memory — Known facts]
#   - (Alice, works_at, Google) [valid 2026-08-27→∞; learned …; conf 0.92;
#      id 3f2a91c2; src #a1b2c3d4; "I work at Google"]
```

---

## Benchmark results — BEAM-style long-horizon memory

Synthetic multi-session conversations (BEAM methodology, arXiv:2510.27246),
10 memory abilities, deterministic nugget judge, **μ=0 ingest asserted**
(zero LLM calls including the judge). Mean ± sd across five generator
seeds (42 / 44 / 45 / 46 / 47 — the last two never inspected during
development), fully reproducible offline:

| Bucket | questions | **Context-M** | BM25-RAG | vector-only |
|---|---|---|---|---|
| 128K | 37 | **100.0% ± 0.0%** | 70.2% | 69.0% |
| 500K | 72 | **100.0% ± 0.0%** | 70.5% | 67.9% |
| 1M | 107 | **100.0% ± 0.0%** | 68.8% | 70.1% |
| **10M** | 216 | **100.0% ± 0.0%** | 61.6% | 66.1% |

Per-seed 10M scores: 100.0% / 100.0% / 100.0% / 100.0% / 100.0% — all
**ten abilities at 100.0%** at the 10M bucket. Context: the plan targeted
**70%+ at BEAM-10M**; the August-2026 SOTA it cites is Exabase M-1 at
**68.0%** (LLM-in-loop ingest). Every probe is answered from a
hash-verified provenance chain, at $0 LLM cost.

Engineering facts measured alongside (see `docs/BENCHMARKS.md`):

- **Ingest:** 10M tokens in ~98 s (**~102K tokens/s**), ~2,000 messages/s, 0 LLM calls
- **Memory grows sublinearly:** 10M tokens → ~590 facts (repeated noise dedupes)
- **Provenance:** 100% of retrieved facts hash-verified; audit latency ~6 ms
- **Retrieval:** tree index p50 ≈ 0.4–1.1 ms at 10K–100K vectors (flat: 16–194 ms)
- **Reproducible:** runs are process-independent — score ties break on fact
  content, never on random ids (verified across four PYTHONHASHSEED values)

## The architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                      THE BRIDGE (μ = 0)                          │
│  write: text → chunks → BLAKE3 → patterns → triples → holograms  │
│  read:  query → intent plan → VSA probe ∥ symbolic query →       │
│         fusion → [Memory — Known facts] + provenance chain       │
└──────────────┬───────────────────────────────────┬───────────────┘
               │                                   │
┌──────────────▼──────────────────┐ ┌──────────────▼───────────────┐
│  LAYER 1: SYMBOLIC TRACE        │ │  LAYER 2: VSA MEMORY PALACE  │
│  (hippocampus)                  │ │  (neocortex)                 │
│  bi-temporal facts (SQLite)     │ │  HRR holograms, role-bound   │
│  CONTRADICTS / PRECEDED_BY /    │ │  INT8 · Binary · RaBitQ · PQ │
│  EXTRACTED_FROM edges           │ │  codecs (770/96/96/8 B each) │
│  Datalog-lite rules engine      │ │  page-clustered tree index   │
│  interference-aware lifecycle   │ │  64-entry semantic L1 (SLB)  │
│  Memory Git: hash-chained DAG   │ │  TMR self-healing + re-encode│
└─────────────────────────────────┘ └──────────────────────────────┘
```

**Layer 1 — Symbolic Trace.** Subject-Relation-Value triples with
valid-time *and* transaction-time (`when it was true` vs `when we
learned it`), contradiction resolution by truth maintenance (new values
supersede, old values retire with their windows intact), temporal
edges, a Datalog-lite forward-chaining engine (`manages(Y,X) →
reports_to(X,Y)`, `member_of(X,T) ∧ uses(T,L) → team_uses(X,L)`), and
an interference-aware lifecycle: facts are evaluated for how they
interact with existing memory *before* commitment.

**Layer 2 — VSA Memory Palace.** Each fact becomes a holographic
reduced representation: role-bound subject/relation/value fillers
plus a λ-weighted lexical superposition, quantized to your storage
tier. Permutation binding is the default algebra because it maps
directly to binary HDC hardware (XOR/permutation) — when edge ASICs
arrive, the same code compiles down.

**The Bridge.** μ=0 ingest: a 60-pattern deterministic extractor
(first/third/second-person, pronoun resolution, relative dates,
retractions) — no LLM anywhere on the write path. The read path is a
deterministic query planner (temporal windows, ordering proofs,
counting, supersession chains, **Personalized PageRank graph diffusion**
for multi-hop — HippoRAG 2 lineage) fused with VSA retrieval, and
**every returned fact carries its full audit chain**: query → VSA match
→ symbolic dereference → BLAKE3 hash → original source text.

## The five category-defining features

| Feature | What it does | Try it |
|---|---|---|
| **Memory Git** | branch / merge / diff / blame over agent memory, hash-chained commits | `examples/07_memory_git.py` |
| **ZK-lite proofs** | prove a fact matches a query without revealing it to the LLM | `examples/08_zk_proof.py` |
| **Self-healing memory** | bit flips detected by hash, TMR majority vote, re-encode from Trace — 100% self-ID up to 10% corruption | `examples/09_self_healing.py` |
| **Predictive prefetching** | MBTB co-access prediction feeds the fusion boost set | `context_m/features/prefetch.py` |
| **Cross-modal binding** | episodic holograms: bind text/structured/sensor roles, recall by any modality | `context_m/vsa/ops.py` |

## Storage tiers (cortexm-compress)

| Tier | Bytes/vector | 1M memories | Fits on |
|---|---|---|---|
| `int8` (default) | 770 | 770 MB | any laptop |
| `binary` + TMR | 96 (288 w/ TMR) | 96 MB | Raspberry Pi 5 → 10M memories |
| `rabitq` | 96 | 96 MB | Raspberry Pi Zero 2W |
| `pq` | 8 | 8 MB | cloud, billions |

Measured codec quality (20K fact holograms): int8 overlap@10 vs FP32 =
0.90; binary/rabitq/PQ recover the FP32 top-10 within their top-50 at
1.00/1.00/0.9995 — shortlist codecs, exactly as designed. See
`docs/COMPRESSION.md`.

## Security (InjecMEM + MINJA defense)

Every fact carries a BLAKE3 hash of its source text, re-verified on
retrieval. Memory-injection patterns ("ignore all previous
instructions…") are quarantined at ingest — stored for audit, never
active, never retrieved into prompt context. On top of that, the
**MINJA contagion guard** treats quarantined text as a tainted corpus:
any later ingest that quotes or substantially overlaps it (even when
light edits defeat every regex) is quarantined too — closing the
query-only injection loop where an attacker poisons memory through the
agent's own write-back. Scopes (user/agent/run) sandbox facts and the
retrieval cache alike; `verify_integrity()` audits the whole store.

## Enterprise controls (shipped, not roadmap)

The controls a buyer's security review actually blocks on — all in the
repo, all under test (`tests/test_enterprise.py`):

| Control | What ships |
|---|---|
| **PII firewall** | Luhn/mod-97/area-rule-validated detection of emails, phones, cards, SSNs, IBANs, IPs, API keys — redacted to reversible vault tokens *before* extraction (GDPR/CCPA write-path guard) |
| **Encryption at rest** | AES-256-GCM envelope (KEK→DEK), key rotation, env/keyfile/sidecar master keys |
| **RBAC + API keys** | admin / operator / reader / auditor roles, peppered-key digests, TTLs, constant-time verify |
| **Tamper-evident audit** | hash-chained per-operation log; SIEM export (JSONL + syslog); tampering pinpoints the broken seq |
| **GDPR governance** | Art. 17 right-to-erasure with crypto-shredding + attestation; Art. 5 retention policies; DSAR vault resolution |
| **Backup / DR** | atomic snapshots with SHA-256 manifests; **PITR** — bi-temporal replay, the database is its own WAL |
| **REST API** | 20 endpoints, OpenAPI 3.1 at `/openapi.json`, bearer auth, per-key rate limiting, Prometheus `/metrics`, `/healthz` `/readyz` |
| **Deploy anywhere** | Docker (non-root, tini, healthcheck) · docker-compose + nightly snapshots · K8s manifests · Helm chart — `deploy/` |

```bash
cortexm serve-rest --db /data/memory.db --pii redact --admin-key yes
```

See `docs/ENTERPRISE.md` (control matrix + compliance mapping) and
`docs/DEPLOYMENT.md` (SDK / MCP / REST / Docker / K8s / Helm runbooks).

## MCP server (Day 1)

```bash
cortexm serve        # stdio JSON-RPC, zero dependencies
```

Tools: `contextm_add`, `contextm_search`, `contextm_get_all`,
`contextm_history`, `contextm_temporal`, `contextm_audit`,
`contextm_prove`, `contextm_stats`, `contextm_delete`. Works with
Claude Code / Cursor / any MCP client. Claude Code plugin:
[`plugins/context-m-claude`](plugins/context-m-claude).

## Migration

```bash
cortexm migrate --from mem0 --path mem0.db
cortexm migrate --from zep --path zep_export.jsonl
cortexm migrate --from chroma --path chroma.sqlite3
```

## More

- `docs/ARCHITECTURE.md` — every layer in detail
- `docs/BENCHMARKS.md` — full results, methodology, per-ability tables
- `docs/ENTERPRISE.md` — enterprise control matrix + compliance mapping
- `docs/DEPLOYMENT.md` — SDK / MCP / REST / Docker / K8s / Helm runbooks
- `docs/RESEARCH.md` — literature lineage: every paper we adopted,
  aligned with, or rejected (with reasons)
- `docs/SECURITY.md` — InjecMEM + MINJA defenses, provenance model
- `docs/COMPRESSION.md` — the tier stack and measured trade-offs
- `docs/ROADMAP.md` — phase status vs the strategic plan
- `examples/` — 10 runnable scripts, offline, no API keys
- `tests/` — 63 tests: fabric + enterprise + PPR + concurrency

## License

Apache 2.0 — open core done right: the memory fabric is and stays open;
federated sync and the audit UI are the enterprise tier.
