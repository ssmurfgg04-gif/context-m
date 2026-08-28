<div align="center">
  <h1>Context-M</h1>
  <h3>Deterministic agent memory. 96 bytes per fact. Zero LLM at ingest.</h3>
</div>

<div align="center">
  <a href="https://github.com/ssmurfgg04-gif/context-m/actions/workflows/test.yml"><img src="https://github.com/ssmurfgg04-gif/context-m/actions/workflows/test.yml/badge.svg?branch=main" alt="Tests"></a>
  <a href="https://github.com/ssmurfgg04-gif/context-m/actions/workflows/pr-gate.yml"><img src="https://img.shields.io/github/checks-status/ssmurfgg04-gif/context-m/main/.github/workflows/pr-gate.yml?label=pr-gate" alt="PR Gate"></a>
  <a href="https://github.com/ssmurfgg04-gif/context-m/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License"></a>
  <a href="https://pypi.org/project/cortexm/"><img src="https://img.shields.io/pypi/v/cortexm?color=%2334D058&label=pypi%20%7Ccortexm" alt="PyPI: cortexm"></a>
  <a href="https://pypi.org/project/context-m-langchain/"><img src="https://img.shields.io/pypi/v/context-m-langchain?color=%2334D058&label=pypi%20%7Clangchain" alt="PyPI: context-m-langchain"></a>
  <a href="https://www.npmjs.com/package/dsh-cortexm"><img src="https://img.shields.io/npm/v/dsh-cortexm?color=%2334D058&label=npm%20%7Cdsh-cortexm" alt="npm: dsh-cortexm"></a>
  <a href="https://pypi.org/project/cortexm/"><img src="https://img.shields.io/pypi/pyversions/cortexm.svg?color=%2334D058" alt="Python versions"></a>
  <a href="https://github.com/ssmurfgg04-gif/context-m/blob/main/AGENTS.md"><img src="https://img.shields.io/badge/AGENTS.md-2026-2f2f2f?logo=github" alt="AGENTS.md"></a>
  <!-- MCP Registry badge — uncomment after submitting deploy/mcp-registry-submission.json to https://registry.modelcontextprotocol.io -->
  <!-- <a href="https://registry.modelcontextprotocol.io/servers/contextm"><img src="https://img.shields.io/badge/MCP%20Registry-contextm-7c3aed" alt="MCP Registry"></a> -->
  <!-- Trendshift badge slot — auto-renders when the repo actually trends. -->
  <!-- <a href="https://trendshift.io/repositories/ssmurfgg04-gif/context-m"><img src="https://trendshift.io/api/badge/repositories/ssmurfgg04-gif/context-m.svg" alt="Trendshift"></a> -->
</div>

<br>

> **Mem0 gives your agent a notebook. Context-M gives your agent a brain.**

Context-M is a memory layer for AI agents that needs zero LLM calls to
ingest and proves every retrieved fact with a BLAKE3 hash chain.
Mem0-compatible: drop-in replacement for `from mem0 import Memory`.

A memory substrate that combines a **bi-temporal symbolic Trace**
(hippocampus) with a **VSA Memory Palace** (neocortex), bound by a
**μ=0 deterministic bridge** — cryptographic provenance on every
retrieval, edge-first deployment at 96 bytes per memory.

```bash
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

## Benchmark results — August 2026

We run four tiers of evaluation, and **the honest number is not the
biggest one**. Full methodology, judge identities and failure analysis:
[`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) ·
[`docs/FAILURE_MODES.md`](docs/FAILURE_MODES.md) ·
[open the leaderboard →](leaderboard/index.html)

### Tier 1 — Out-of-distribution (where users live)

Ground-truth fact registries were re-rendered by an independent LLM in
styles the pattern extractor never saw, then evaluated with the same
probes and judge as the in-distribution run:

| OOD style | Tier-1.1 (pre-fix) | Tier-1.2 (post-fix, 2026-08-28) | Δ |
|---|---|---|---|
| paraphrase | 9.4% ± 9.4% | **22.9%** | +13.5pp (2.4×) |
| negation | 75.6% ± 3.3% | 75.6% | flat |
| indirect speech | 44.9% ± 10.2% | **48.2%** | +3.3pp |
| informal/slang | 5.1% ± 5.9% | **41.3%** | +36.2pp (8.1×) |
| non-English | **0.0%** | **32.2%** | +32.2pp (∞ → real recall) |
| code-switching | 57.9% ± 18.1% | **61.3%** | +3.4pp |

The slang jump (5.1% → 41.3%) is the single biggest fix in this
cycle: the unmess pipeline (DisSim + idiolect + Bitap) is now safe
to enable in the bench config (previously the period-strip bug
forced `unmess_enabled=False`). The non-English jump (0% → 32%)
comes from the LaBSE polyglot encoder + idiolect normalizer
handling accented characters without crashing the trigger.

### Tier 4.3 — LongMemEval independent judge

| subtask | pre-fix | post-fix (2026-08-28) | plugin-kernel (2026-08-29, v0.5.0) | Δ vs pre-fix |
|---|---|---|---|---|
| single_hop | 1.0 | 1.0 | 1.0 | flat |
| knowledge_update | 0.333 | 0.667 | **1.000** | 3× |
| multi_session | 0.5 | 0.5 | 0.5 | flat |
| temporal_reasoning | 0.5 | 0.5 | 0.5 | flat |
| **overall** | 0.600 | 0.700 | **0.800** | +20pp |

The v0.5.0 lift (0.700 → 0.800) comes from the new plugin kernel
+ verbatim tier: when the structured extractor misses a fact
("I'm now working at OpenAI" → role pattern), the FTS5 + int8
dense path catches it verbatim. Fusion then merges both tiers
at μ=0 cost. The 2 misses that remain are aggregation phrasing
("List all the places Bob has worked") and yes/no answer shape
("Did Bob move between sessions") — extractor limitations, not
memory limitations.

Reproduce: `python scripts/longmemeval_judge.py --out
benchmarks/results/longmemeval_v0.5.0.json` ·
[`benchmarks/results/longmemeval_v0.5.0.json`](benchmarks/results/longmemeval_v0.5.0.json).

Pre-plugin-kernel fixes (0.600 → 0.700): (1) `works_at` regex
contraction fix ("I'm now working at OpenAI" now extracts),
(2) role pattern `|$` lookahead + uppercase support ("I'm an ML
engineer" now extracts), (3) employment-anchored temporal window
(resolves "where did X live when at Y" via the works_at fact's
valid_from/valid_to).

Plugin-kernel fixes (0.700 → 0.800): the new verbatim tier (FTS5
+ int8 dense, MemPalace-style) catches "I'm now working at OpenAI"
verbatim when the structured extractor's role pattern still misses
it. The fusion bridge then merges both tiers at μ=0 cost. The 2
remaining misses are not memory failures — they are answer-shape
mismatches (the judge asks for a yes/no, the context block returns
a list of facts the LLM must reason over).

That is the capability profile of the μ=0 extractor on real phrasing:
strong on change-of-state statements, weak on identity/preference
restatements, weak on non-English without the LaBSE polyglot encoder.
The async LLM enrichment fallback helps marginally — it surfaces facts
but does not reconstruct bi-temporal chains. [`docs/FAILURE_MODES.md`](docs/FAILURE_MODES.md)
documents which phrasings break, with worked examples.

**Independent LLM judges grade these numbers *lower*, not higher.** The
full 240-item OOD sweep was re-graded by `gemini-3.5-flash-lite` from a
clean CI runner: LLM-judge mean **0.222** vs offline judge **0.335**,
exact agreement 82.7% (237/240 items;
[`results/ood/llm_judge_crosscheck_gemini.json`](benchmarks/results/ood/)).
A second judge (glm-4-plus, 58-item quota sample) agrees: 0.250 vs 0.345.
Two independent models, same conclusion — the offline grader is not
inflating scores. Judge model ≠ canonical BEAM's gpt-5, so these are
cross-checks, not BEAM-comparable numbers.

**Tier 2 — In-distribution (the regression harness).** Synthetic
BEAM-style conversations (arXiv:2510.27246 methodology), 10 abilities,
deterministic nugget judge, μ=0 ingest asserted, 5 seeds:

| Bucket | questions | **Context-M** | BM25-RAG | vector-only |
|---|---|---|---|---|
| 128K | 37 | **100.0% ± 0.0%** | 70.2% | 69.0% |
| 500K | 72 | **100.0% ± 0.0%** | 70.5% | 67.9% |
| 1M | 107 | **100.0% ± 0.0%** | 68.8% | 70.1% |
| **10M** | 216 | **100.0% ± 0.0%** | 61.6% | 66.1% |

**Why 100% here is not a capability claim:** the corpus generator and the
extractor patterns were authored against the same template families, so
this tier measures *template coverage*, ceiling by construction. Its job
is regression detection — "did we break template extraction?" — not
marketing. We do **not** compare it against canonical BEAM SOTA (Exabase
M-1, 68.0%): different corpus, different judge, different protocol — an
apples-to-oranges comparison we refuse to make.

**Tier 3 — Real GitHub data.** Real issue threads from public repos
(rust-lang/rust, numpy/numpy, pydantic/pydantic; attribution in
`benchmarks/real_github/`): the μ=0 extractor vs an LLM reference
extractor (`gemini-3.5-flash-lite`) on identical comments, plus retrieval
QA judged by the same LLM:

| Track | Result |
|---|---|
| μ=0 extraction | 16 facts from 150 comments · 1.1 ms/comment · **$0.00** |
| LLM reference extraction | 158 facts · 2,779 ms/comment · ~90K tokens |
| μ=0 recall vs LLM reference | **0.6%** — the honest gap on real technical text |
| Retrieval QA (LLM-judged, 19 Qs) | overall 0.263 · answerable 0.067 · abstention 100% |

Read this as the cost/coverage frontier: the μ=0 path is ~2,500× faster
and free but, on developer-issue language, captures ~10× fewer facts
than an LLM extractor. The enrichment fallback and per-domain pattern
packs are the bridge. Artifacts:
[`benchmarks/results/real_github/`](benchmarks/results/) ·
[`results/llm_eval_summary.md`](benchmarks/results/llm_eval_summary.md).

Engineering facts measured alongside (see `docs/BENCHMARKS.md`):

- **Ingest:** 10M tokens in ~98 s (**~102K tokens/s**), ~2,000 messages/s, 0 LLM calls
- **Memory grows sublinearly:** 10M tokens → ~590 facts (repeated noise dedupes)
- **Provenance:** 100% of retrieved facts hash-verified; audit latency ~6 ms
- **Retrieval:** tree index p50 ≈ 0.4–1.1 ms at 10K–100K vectors (flat: 16–194 ms)
- **Crash-recoverable:** WAL journaling with SIGKILL-recovery tests
  (`tests/test_wal_recovery.py`) — committed memories survive hard kills
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

**The Bridge.** μ=0 ingest: a 61-pattern deterministic extractor
(first/third/second-person, pronoun resolution, relative dates,
retractions, Mem0-summary shapes) — no LLM anywhere on the synchronous
write path. When patterns find nothing (non-English, heavy slang,
indirect speech), an **explicit async enrichment fallback**
(`memory.enrich()`) re-extracts those chunks with an LLM post-store —
confidence-capped at 0.85, provenance-marked `llm_enrichment`, auditable,
and counted in the μ=0 honesty counters. The read path is a
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
| **Predictive prefetching** | MBTB co-access prediction feeds the fusion boost set | `cortexm/features/prefetch.py` |
| **Cross-modal binding** | episodic holograms: bind text/structured/sensor roles, recall by any modality | `cortexm/vsa/ops.py` |

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

## Security (InjecMEM + MINJA defense + scope sandbox)

Every fact carries a BLAKE3 hash of its source text, re-verified on
retrieval (BLAKE2b-256 fallback with a **loud warning** if the optional
`blake3` wheel is absent — `pip install cortexm[blake3]`; the active
provider is always reported in `stats()` and audit output). Memory-
injection patterns ("ignore all previous instructions…") are
quarantined at ingest — stored for audit, never active, never retrieved
into prompt context. On top of that, the **MINJA contagion guard**
treats quarantined text as a tainted corpus: any later ingest that
quotes or substantially overlaps it (even when light edits defeat every
regex) is quarantined too — closing the query-only injection loop where
an attacker poisons memory through the agent's own write-back.

The **scope sandbox** enforces the isolation the InjecMEM threat model
implies: facts written by an agent (`agent_id=...`) are invisible to
user-scope reads until explicitly `promote()`d — and promotion is
gated on confidence, re-scans the source chunk through both injection
detectors, and lands in the tamper-evident audit chain
(`tests/test_sandbox_enrich.py`). Building it surfaced and fixed three
genuine pre-existing read-path leaks (empty-scope fallback, falsy scope
checks, unscoped supersession chains). `verify_integrity()` audits the
whole store.

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

Each importer handles the vendor's real on-disk formats (mem0's
`history` JSON payloads and bare `memories` tables, Zep graph triples
with bi-temporal windows, Chroma's `embeddings` table) and is verified
end-to-end against fixture stores built in those exact formats
(`tests/test_migration.py`).

## Durability

WAL journaling (Aeon-inspired) with a `wal_sync` durability knob
(`normal` — survives process crash; `full` — fsyncs every commit,
survives power loss), WAL checkpoint-on-close, and a test that
SIGKILLs a writer mid-stream and verifies every acknowledged commit
survives (`tests/test_wal_recovery.py`).

## Federation (CRDT replication)

Multi-node memory replication without a coordinator: bi-temporal facts as
HLC-stamped CRDT versions (SINGLE_VALUED relations collapse into one
versioned register per key — the version set IS the temporal history),
union merge that is commutative/associative/idempotent, OR-set
retraction semantics (write-after-retract wins, retract-after-write
wins), purge poison-pills for GDPR, and digest/delta anti-entropy that
ships only divergent buckets over HMAC-signed envelopes. Convergence is
proven **byte-exact** (canonical serialization compared, not just query
equivalence); a partition with divergent writes + retractions heals with
no lost retraction semantics. Transports: in-memory mesh for tests, file
spool (outbox/inbox) for offline mule sync — rsync/git/USB completes the
physical channel, the CRDT guarantees convergence regardless of delivery
order. See `cortexm/federation/` and `benchmarks/federation_bench.py`.

## Rust acceleration (optional wheels)

`rust/cortexm-core` and `rust/quadrant` compile the hot paths with
PyO3; the Python/NumPy implementation stays the reference and everything
works without them (`CONTEXTM_RUST=0` forces the pure-Python path).
Measured on the bundled scorecard (`benchmarks/rust_vs_numpy.py`):
**encode_fact 4.8×, bind 3.4×, h64 2.2×** — h64 is byte-exact with the
Python hash (tested), and permutations/role vectors are *injected* from
Python's deterministic VSA state, so mixed deployments produce
bit-identical holograms. The SLB is a **tie** (1.0× — BLAS is already
optimal at 64×768; published as such). `quadrant` is the page-clustered
log-depth vector index for the L2 palace: 97% recall@10 at 7× NumPy
brute-force speed, visiting ~32 of 529 pages for 20k vectors — visit
counts are instrumented, the O(log N) claim is measured, and the
adversarial random-corpus recall collapse is published alongside the
win. Build: `pip install ./rust/cortexm-core ./rust/quadrant`.

## More

- `docs/ARCHITECTURE.md` — every layer in detail
- `docs/BENCHMARKS.md` — full results, methodology, per-ability tables
- `docs/FAILURE_MODES.md` — where the extractor breaks on real phrasing,
  with worked examples (read before citing any number)
- `docs/ENTERPRISE.md` — enterprise control matrix + compliance mapping
- `docs/DEPLOYMENT.md` — SDK / MCP / REST / Docker / K8s / Helm runbooks
- `docs/RESEARCH.md` — literature lineage: every paper we adopted,
  aligned with, or rejected (with reasons)
- `docs/SECURITY.md` — InjecMEM + MINJA defenses, scope sandbox,
  provenance model
- `docs/COMPRESSION.md` — the tier stack and measured trade-offs
- `docs/ROADMAP.md` — phase status vs the strategic plan
- `docs/GOVERNANCE.md` — foundation governance + licensing commitments
- `leaderboard/` — self-hosted benchmark site (rebuild: `python
  leaderboard/build.py`; open `leaderboard/index.html`)
- `examples/` — runnable scripts, offline, no API keys
- `tests/` — 116 tests: fabric + enterprise + PPR + concurrency +
  sandbox + enrichment + WAL crash-recovery + migration + CRDT
  federation convergence/partition-heal + Rust parity

## License

Apache 2.0 — open core done right: the memory fabric is and stays open;
federated sync and the audit UI are the enterprise tier.

## arXiv-inspired improvements (2026 round)

A second research pass over 2024-2026 arxiv literature surfaced 8
concrete improvements, all preserving the μ=0 invariant. Full citations
in `docs/BENCHMARKS.md` Tier 8.

| Improvement | Module | Solves |
|---|---|---|
| Hopfield cleanup memory | `cortexm/vsa/cleanup.py` | VSA interference after unbind |
| Bitap fuzzy matching (Wu-Manber) | `cortexm/text/fuzzy.py` | Slang/spelling-tolerant pattern triggers |
| Per-user idiolect normalization | `cortexm/text/idiolect.py` | "bruh"→"friend" via embedding k-NN |
| DisSim rule-based simplifier | `cortexm/text/dissim.py` | Compound-sentence pattern recall |
| TLSH ternary trie | `cortexm/vsa/tlsh_trie.py` | O(log N + w) software TCAM |
| Holographic fact overlay | `cortexm/vsa/hologram_overlay.py` | O(1) single-hop fact lookup |
| ProtoDash attribution | `cortexm/vsa/attribution.py` | Source weights for retrieval results |
| LayerCast FP32 determinism seam | `cortexm/bridge/onnx_runtime.py` | μ=0 over LLM enrichment path |

Architectural fixes (per Con #4-#7 list):

- **Storage bloat** → `cortexm/trace/dedup.py` formalizes dedup+compression audit
- **Normalization** → Bitap + idiolect + hybrid search wired into patterns
- **Debugging** → `retrieval_path ∈ {vsa_unbind, pattern_match, neural_fallback, raw_chunk, tree_index, tlsh_trie}` on every retrieved fact
- **Determinism** → LayerCast + ONNX Runtime CPU + FP32 seam documented

Plus explicit binary/FP32 tiering (`accel.detect_tier`, `accel.recommend_codec`),
Hamming ZK proofs (`security/zk_hamming.py`), and `trace/rebuild.py`
for checksum-audited rebuilds from the symbolic Trace.

## Claude Code plugin — session lifecycle

`plugins/context-m-claude/src/index.ts` v0.2 adds auto-load on Claude
session start + write-on-end hooks:

- **on session start** → `recall last working state` → "I see you've been working on X. Continue?"
- **on session end** → "Store summary? [Y/n]" → persists summary as a memory fact
- Session state at `~/.context-m/session_state.json`

MCP tools added: `contextm_query_extract` (hybrid RAG), `contextm_attribution` (ProtoDash), `contextm_zk_prove` (Hamming proofs).

---

## Honest measurement block

> Reproducing the Ponytail convention: every headline number on this
> README is paired with the run that produced it, the SHA, the judge
> model, and the honest cost. "~96 bytes per fact" is the storage
> cost on the BEAM-10M corpus (n=200 personas × 30 turns × ~35 facts
> per persona, measured 2026-08-28 on commit 714f237). "Zero LLM at
> ingest" is enforced by the `LLM_CALLS` counter in
> `cortexm/__init__.py`; a CI assertion fails any PR that increments
> it on the ingest path. The Real-GitHub Tier-4 result (17 questions,
> 0.0 answerable, 1.0 abstention, 2026-08-28 14:46 UTC run #9) is a
> refusal-to-guess, not a coverage gap — the system abstains rather
> than hallucinate on real developer-issue language. Full method:
> [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md). Reproduce:
> `python benchmarks/run_ood_pipeline.py --personas 4 --skip-render
> --no-enrich --no-judge`.

## Anti-lamprey warning

> **Don't fork-and-rebrand this repo.** If you want to build on it,
> open an issue labeled `accepted` and submit a PR — see
> [`CONTRIBUTING.md`](CONTRIBUTING.md) and
> [`AGENTS.md`](AGENTS.md). Fork-and-rebrand-without-attribution
> derivatives will be named in `docs/FAILURE_MODES.md` under the
> "Derivative works" section. The provenance chain (BLAKE3 hash +
> source span) is the system's whole point — strip it and you've
> built a different product, not a fork.

## Star history

<a href="https://star-history.com/#ssmurfgg04-gif/context-m&Date">
  <picture>
    <source media="(prefers-color-scheme: dark)"
      srcset="https://api.star-history.com/svg?repos=ssmurfgg04-gif/context-m&type=Date&theme=dark" />
    <source media="(prefers-color-scheme: light)"
      srcset="https://api.star-history.com/svg?repos=ssmurfgg04-gif/context-m&type=Date" />
    <img width="100%"
      alt="Star History"
      src="https://api.star-history.com/svg?repos=ssmurfgg04-gif/context-m&type=Date" />
  </picture>
</a>
