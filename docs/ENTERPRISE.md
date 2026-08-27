# Context-M — Enterprise Readiness

This document maps every enterprise control Context-M ships to the
procurement question it answers. Everything below is implemented in the
repository and covered by the test suite (`tests/test_enterprise.py`,
58+ assertions) — none of it is roadmap.

> **Deployment baseline**: `pip install cortexm` →
> `cortexm serve-rest --db /data/memory.db` → HTTPS in front. Docker,
> docker-compose, Kubernetes and Helm artifacts live in `deploy/`.

---

## Control matrix

| Buyer question | Control | Module | Status |
|---|---|---|---|
| "Can personal data leak into the memory store?" | PII detection + redaction on the write path | `security/pii.py` | ✅ shipped |
| "Is the database encrypted at rest?" | AES-256-GCM envelope encryption (KEK/DEK) | `security/crypto.py` | ✅ shipped |
| "Who can read/write memory?" | RBAC: admin / operator / reader / auditor + API keys | `security/rbac.py` | ✅ shipped |
| "Can you prove who did what?" | Hash-chained tamper-evident audit log + SIEM export | `enterprise/audit.py` | ✅ shipped |
| "Can you delete a user on request (GDPR Art. 17)?" | Right-to-erasure with crypto-shredding + attestation | `enterprise/governance.py` | ✅ shipped |
| "How long do you keep data (Art. 5(1)(e))?" | Retention policies with dry-run | `enterprise/governance.py` | ✅ shipped |
| "Backup / DR?" | Atomic snapshots with integrity manifests + restore | `enterprise/governance.py` | ✅ shipped |
| "Point-in-time recovery?" | Bi-temporal PITR — the DB is its own WAL | `enterprise/governance.py` | ✅ shipped |
| "Observability?" | Prometheus `/metrics`, JSON logs, `/healthz` `/readyz` | `server/metrics.py`, `server/rest.py` | ✅ shipped |
| "Can our SIEM ingest events?" | JSONL + RFC-3164 syslog exports | `enterprise/audit.py` | ✅ shipped |
| "HTTP API + OpenAPI?" | 20-endpoint REST surface, OpenAPI 3.1 at `/openapi.json` | `server/rest.py` | ✅ shipped |
| "Rate limiting / abuse control?" | Token-bucket per API key | `server/rest.py` | ✅ shipped |
| "Multi-threaded safe?" | Serialized connection wrapper | `trace/store.py` (`SafeConnection`) | ✅ shipped |
| "Supply-chain posture?" | Zero runtime dependencies (stdlib + numpy); AES via `cryptography` | `pyproject.toml` | ✅ shipped |

---

## 1. PII firewall (GDPR / CCPA)

Personal data is stopped **before** extraction, so raw PII never reaches
facts, chunks, vector codes, or the SLB cache.

```python
from context_m import Memory
m = Memory(pii_mode="redact")          # off | redact | block | tag
m.add("My email is alice@corp.com and I work at Google.")
# facts/chunks contain: «PII:EMAIL:0001» … Google
m.pii_vault.resolve("«PII:EMAIL:0001»")   # DSAR re-identification → alice@corp.com
```

Detectors (regex + checksum, zero LLM calls — μ=0 intact):
email, phone (intl), credit card (**Luhn-validated**), SSN (**area/group
rules**), IBAN (**mod-97**), IP, API keys (`sk-`, `ghp_`, `xox`, `AKIA`,
`AIza`), passport-shaped ids. Invalid checksums are rejected —
`4111 1111 1111 1112` is not PII, it's a typo.

Modes: `redact` (tokenize + vault), `block` (refuse write, audited),
`tag` (annotate only), `off` (dev / benchmark).

**Vault**: tokens map back to originals through a vault table,
AES-256-GCM-encrypted when a master key is present. Crypto-shredding the
vault on erasure makes every historical token permanently
unrecoverable.

## 2. Encryption at rest

Envelope scheme: master key (KEK) from `CONTEXT_M_MASTER_KEY` env, key
file, or auto-generated sidecar (`<db>.key`, 0600) → per-database data
key (DEK) wrapped by the KEK, stored inside the DB (`enc:dek`).

* A stolen DB file without the master key yields no plaintext.
* Key rotation re-wraps the DEK without rewriting payloads.
* Wrong master key ⇒ explicit `CryptoUnavailable` on open, never silent
  garbage.

## 3. RBAC + API keys

```
ctxm_operator_b95c95837fda07acb3bb824338bf0a14
```

Keys are stored **only** as BLAKE2b digests with a per-deployment
pepper — a leaked database cannot be used to authenticate. Verification
is constant-time. Roles: `admin` (all), `operator` (memory R/W +
snapshots), `reader` (search/get), `auditor` (audit/verify only).
Destructive actions (`delete_all`, erasure, restore, key management)
are admin-only. TTLs supported on every key.

## 4. Tamper-evident audit chain

Every security-relevant operation appends
`{seq, ts, actor, role, action, resource, outcome, meta, prev_hash,
hash}` where `hash = BLAKE2b(prev_hash || canonical_record)`. Deleting
or editing any row breaks the chain — `verify()` reports the first
damaged sequence number.

Exports: JSONL (Splunk/Elastic/Datadog) and RFC-3164-style syslog.
The audit chain is intentionally exempt from erasure (legal records) —
it stores actor and resource ids, never conversation text.

## 5. GDPR governance

**Erasure (Art. 17)** — `erase_user()` removes facts, chunks, vectors,
edges, lexicon, aliases, and crypto-shreds the PII vault; it leaves a
tamper-evident **erasure attestation** in the audit chain and returns a
residual-scan report proving zero remaining rows.

**Retention (Art. 5(1)(e))** — `apply_retention(days)` expires stale
facts with bi-temporal tombstones (`dry_run` supported).

**PITR** — `state_at(when)` replays "what did the system believe at
T" from transaction times alone; the database is its own write-ahead
log. No snapshot infrastructure required for forensic reads.

## 6. Operations

* `/healthz` liveness, `/readyz` readiness (real store ping)
* `/metrics` Prometheus text format (counters + latency histograms +
  the μ=0 honesty gauge `contextm_llm_calls_total`)
* Nightly snapshot CronJob with 7-copy retention (K8s/compose shipped)
* `POST /v1/snapshot` → atomic SQLite online-backup + SHA-256 manifest;
  `POST /v1/restore` refuses mismatched digests

## 7. Hardening details

* **Thread safety**: one SQLite connection guarded by a serialized
  wrapper (eager row materialization) — concurrent REST writers and
  readers are covered by tests.
* **Rate limiting**: token bucket per key (default 50 rps, burst 100).
* **Body cap**: 8 MiB request limit.
* **Auth failures are audited** (`auth.failure`) with key-prefix
  tracing, never the full key.

## 8. Compliance mapping (guide, not legal advice)

| Framework | Coverage shipped |
|---|---|
| GDPR | Art. 5 (retention), Art. 17 (erasure), Art. 30 (records = audit chain), Art. 32 (encryption, integrity) |
| CCPA/CPRA | deletion + access (DSAR via vault resolution) |
| SOC 2 (Security/Availability/Confidentiality) | RBAC, audit chain, encryption, backups, monitoring |
| HIPAA (technical safeguards) | §164.312(a)(1) access control, (b) audit controls, (e)(2)(ii) encryption at rest |

## 9. What is deliberately NOT claimed

* No multi-region replication yet — single-node SQLite with snapshots;
  federation schema aggregation exists for cross-node rollups, CRDT
  sync is on the roadmap.
* PII detection is regex/checksum-based: deep paraphrase laundering of
  personal data (a human rewriting it) is out of scope for any
  non-LLM detector; the `tag` mode exists for human-in-the-loop
  review pipelines.
* The REST server speaks plain HTTP — TLS termination belongs to your
  ingress (standard practice; the K8s manifest annotates accordingly).
