# Context-M — Session Memory

Living state file for autonomous continuation. Last updated: enterprise
hardening + perfect-score session, 2026-08-27 (v0.2.0).

## Current state: ENTERPRISE HARDENING COMPLETE, 100% BENCHMARKS

- **Repo:** `/home/z/my-project/context-m/` (sole ownership)
- **Package:** `context_m` v0.2.0 (+ `cortexm` alias)
- **Tests:** 63/63 green (25 fabric + 2 MINJA + 31 enterprise + 5 PPR)
- **Benchmarks (μ=0 asserted, 5 seeds — mean ± sd):**

| bucket | context_m | bm25 | vector_only |
|---|---|---|---|
| 128k | **100.0% ± 0.0%** | 70.2% | 69.0% |
| 500k | **100.0% ± 0.0%** | 70.5% | 67.9% |
| 1m | **100.0% ± 0.0%** | 68.8% | 70.1% |
| 10m | **100.0% ± 0.0%** | 61.6% | 66.1% |

All 10 abilities at 100.0% in all buckets. Seeds 46/47 were never
inspected during development (honest generalization check).

## What this session changed

### 1. Benchmark: 98.3% → 100.0% (five real bugs found)

- **Occupation retrieval (IE)**: "for a living" matched the *residence*
  hint (contains "living") and drowned `role` facts — added an
  occupation-idiom hint FIRST in RELATION_HINTS.
- **role_at_org extractor dropped roles**: "I'm a software engineer at
  Netflix" emitted works_at but silently discarded the occupation — now
  emits both facts.
- **Month-granularity windows never closed**: "in February 2025" set
  window_end=None (open interval dragged in later events); now closes at
  end-of-month. Numeric "2025 08" / ISO "2026-03-15" date forms parse.
- **Inverted bi-temporal intervals**: stale retractions could set
  valid_to < valid_from — clamped in both SUPERSEDE and retraction paths;
  exact-restatement merges now backdate valid_from from "since" clauses.
- **Window tiering**: facts that BEGAN inside a temporal window outrank
  still-valid background facts that merely overlap it.
- **Datalog scope leak (correctness)**: derived facts inherited
  user_id="default" — user0's reader could never see team_uses
  derivations. Rules now join per-scope and derived facts inherit the
  premise scope.
- **Prefetcher cannibalizing precision**: MBTB co-access boosts
  reordered rankings for multihop/list intents — prefetch now only warms
  simple recall/current intents. Added "list" intent (exhaustive set
  recall: "list all the projects…").
- **Multi-hop chain completion**: value-hop expansion boost raised to
  0.7 for multihop intent (the X→Y→Z semantic shape).

### 2. Personalized PageRank read mode (HippoRAG 2 lineage)

`context_m/bridge/ppr.py` — bipartite entity↔fact graph built locally
per query (bounded 96 nodes), deterministic power iteration, blended
into fusion. Config: `ppr_enabled/damping/iters/weight/graph_size/seeds`.

### 3. Enterprise layer (what buyers' security reviews block on)

- `security/pii.py` — PII firewall: email/phone/card(Luhn)/SSN(rules)/
  IBAN(mod-97)/IP/API-key/passport detectors; off|redact|block|tag;
  reversible AES-encrypted vault; crypto-shredding. Write-path guard in
  `Memory.add` BEFORE extraction.
- `security/crypto.py` — AES-256-GCM envelope (KEK→DEK), rotation, env/
  keyfile/sidecar master keys.
- `security/rbac.py` — 4 roles, peppered key digests, TTLs, 20 actions
  in the permission matrix, constant-time verify.
- `enterprise/audit.py` — hash-chained audit log; tamper detection
  pinpoints the broken seq; JSONL + syslog SIEM exports.
- `enterprise/governance.py` — GDPR erasure (+attestation), retention
  (dry-run), atomic snapshots w/ SHA-256 manifests, verified restore,
  bi-temporal PITR (`state_at`).
- `server/rest.py` — stdlib REST server: 20 endpoints, OpenAPI 3.1,
  bearer auth, token-bucket rate limiting, /metrics (Prometheus),
  /healthz /readyz. `cortexm serve-rest`.
- `server/metrics.py` — Prometheus registry (counters/histograms/gauges).
- `trace/store.py` — SafeConnection: serialized SQLite wrapper (eager
  row materialization) — concurrent writers/readers safe; `iter_kv`,
  `kv_delete`, `edges_of_many`.
- CLI: keys/audit/snapshot/erase/governance commands.
- `deploy/` — Dockerfile (multi-stage, non-root, tini, healthcheck),
  docker-compose + nightly snapshot sidecar, K8s manifests (PVC +
  CronJob), Helm chart.
- Docs: ENTERPRISE.md (control matrix + GDPR/SOC2/HIPAA mapping),
  DEPLOYMENT.md (SDK/MCP/REST/Docker/K8s/Helm runbooks).

## Decisions log (this session)

- Prefetch boost gated to recall/current: a performance cache became a
  correctness hazard for precision intents (same lesson as the SLB
  scope-key bug: caches must never change answer sets).
- PPR graph is built per-query from candidates (bounded), not globally —
  μ=0 intact, no offline index, deterministic.
- tx_from is the MESSAGE timestamp in this codebase (bi-temporal design
  choice) — PITR queries use message-time boundaries, Z-suffix format.
- Card-shaped spans are claimed even on Luhn failure (else digits get
  reinterpreted as phones).
- Audit chain exempt from erasure (legal records), stores ids not text.

## Bug war stories (do not reintroduce)

- **Datalog scope leak**: any derived fact MUST inherit premise scope —
  cross-scope joins silently make facts invisible to readers.
- **The prefetcher reordered precision**: heuristic boosts belong only
  on heuristic-safe intents.
- **Inverted intervals**: any code setting valid_to must clamp against
  valid_from (retraction dates can predate learned facts).
- All prior war stories (SLB scope-keying, uuid4 tie-breaks, bench DB
  reuse, f-string quantifier braces, re.M in judge parser) still apply.

## Next actions

1. LLM reader/judge replication of canonical BEAM (`llm_judge=` slot).
2. Rust port of `vsa/codecs.py` + `vsa/index.py` behind the codec seam.
3. ArcadeDB backend behind `TraceStore`'s API.
4. CRDT federated Trace sync; leaderboard site from results JSON.
5. SSO (SAML/OIDC) federation on top of the RBAC layer.
6. LangChain / LlamaIndex / OpenAI Agents SDK adapters.

## Environment notes

- python3 = 3.12.14 (numpy 2.1.3, scipy 1.14.1, pytest 9.0.2, blake3 +
  cryptography + pyyaml installed via `python3 -m pip install
  --break-system-packages`). Plain `pip` targets python3.13.
- Full benchmark regeneration (1 seed × 4 buckets) ≈ 2.5 min; run per
  seed (`scripts/run_bench_5seeds.py <seed>` then `<seed> aggregate`).
- Background nohup processes get reaped between tool calls — run long
  jobs in foreground chunks.
- Bench artifact path: `benchmarks/results/` (JSON per bucket per seed,
  `variance.json`, `micro.json`, REPORT.md per seed dir).
- GitHub: ssmurfgg04-gif/context-m — push via one-time token URL only;
  never persist the token.
