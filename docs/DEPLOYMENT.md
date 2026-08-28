# Context-M — Deployment Guide

Context-M ships as a single process with one SQLite file. Pick the
surface that matches your integration: **Python SDK**, **MCP server**
(Claude / IDE agents), or **REST API** (microservice). All three share
the same fabric, governance, and audit chain.

---

## 1. Python SDK (embedding)

```bash
pip install cortexm
```

```python
from cortexm import Memory

m = Memory(db_path="/data/memory.db",
           pii_mode="redact",            # GDPR write-path guard
           encryption_at_rest=True)      # AES-256-GCM envelope
m.add([{"role": "user", "content": "I work at Google."}],
      user_id="alice")
m.search("Where does Alice work?", user_id="alice")
```

## 2. MCP server (agent tooling)

```bash
cortexm serve --db /data/memory.db
```

Exposes 9 tools over stdio JSON-RPC (add / search / stats / verify /
git / …) for Claude Code, Cursor, or any MCP client. See
`plugins/context-m-claude/` for a ready-made Claude Code extension.

## 3. REST API (microservice)

```bash
# mint an admin key at boot (printed once — save it)
cortexm serve-rest --db /data/memory.db --port 8900 --admin-key yes
```

Environment:

| Variable | Purpose | Default |
|---|---|---|
| `CONTEXT_M_DB` | SQLite path | `:memory:` |
| `CONTEXT_M_MASTER_KEY` | 256-bit key (env / file / sidecar) | none |
| `CONTEXT_M_MASTER_KEY_PATH` | explicit key file | none |
| `CONTEXT_M_ENCRYPT` | `true` → encryption at rest | `false` |
| `CONTEXT_M_PII_MODE` | `off\|redact\|block\|tag` | `off` |
| `CONTEXT_M_AUDIT` | `security\|all\|none` | `security` |
| `CONTEXT_M_CODEC` | `int8\|binary\|rabitq\|pq` | `int8` |

First requests:

```bash
KEY="ctxm_admin_…"   # from boot log or: cortexm keys create --role admin

curl -X POST localhost:8900/v1/add \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"I work at Google."}],
       "user_id":"alice"}'

curl -X POST localhost:8900/v1/search \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"query":"Where does Alice work?","user_id":"alice"}'
```

Key management, audit, snapshots, erasure:

```bash
cortexm keys create --role reader --label prod-app --db /data/memory.db
cortexm audit tail -n 20 --db /data/memory.db
cortexm audit verify --db /data/memory.db          # chain integrity
cortexm snapshot --db /data/memory.db --path /backups/nightly.db
cortexm erase  --db /data/memory.db --user-id gdpr-subject-17
```

Live docs: `GET /openapi.json` (OpenAPI 3.1, 20 endpoints).
Metrics: `GET /metrics` (Prometheus). Probes: `/healthz`, `/readyz`.

## 4. Docker

```bash
docker build -f deploy/Dockerfile -t context-m:latest .
docker run -p 8900:8900 -v ctxm-data:/data \
  -e CONTEXT_M_MASTER_KEY=$(openssl rand -base64 32) \
  context-m:latest
```

The image is a multi-stage `python:3.12-slim`, runs as non-root uid
10001, uses tini for signal handling, and ships a container
HEALTHCHECK against `/healthz`.

## 5. Docker Compose (with nightly snapshots)

```bash
cd deploy
cp env.example .env        # fill CONTEXT_M_MASTER_KEY
docker compose up -d
```

Brings up the API plus a nightly snapshot sidecar that keeps the last 7
backup envelopes on the host.

## 6. Kubernetes

```bash
kubectl apply -f deploy/k8s/
```

Includes: Secret (master key), PVC, Deployment (non-root, resource
limits, Prometheus scrape annotations, readiness/liveness probes),
Service, and a nightly snapshot CronJob. The strategy is `Recreate` —
SQLite volumes are RWO; scale horizontally by sharding tenants across
StatefulSets (the fabric is per-tenant by design), not by replicating
one volume.

## 7. Helm

```bash
helm install memory deploy/helm \
  --set security.masterKey=$(openssl rand -base64 32) \
  --set security.piiMode=redact
```

Values: `deploy/helm/values.yaml` (image, resources, persistence,
encryption, PII mode, snapshot schedule, metrics).

## 8. Sizing guide

| Profile | Codec | RAM budget | Vector storage (1M facts) |
|---|---|---|---|
| Cloud standard | `int8` | ~512 MB | 770 MB |
| Edge / Pi 5 | `binary` | ~128 MB | 96 MB |
| Ultra-edge | `rabitq` | ~128 MB | 96 MB (94%+ recall) |
| Bulk cold tier | `pq` | ~64 MB | 8 MB |

Measured ingest: **~104K tokens/s** at 10M-token scale (μ=0, zero LLM
calls). Retrieval latency p50 ≈ 0.4–1.1 ms at 10K–100K vectors via the
page-clustered tree index; ~8 ms at 10M-token benchmark scale with full
provenance verification.

## 9. Backup & recovery runbook

1. **Nightly**: `POST /v1/snapshot` (or the shipped CronJob) — atomic
   online backup + SHA-256 manifest.
2. **Verify** a backup before trusting it: the manifest digest is
   checked on restore; mismatches abort.
3. **Restore**: `POST /v1/restore {"path": ...}` — closes handles,
   replaces the file, reopens, audits the event.
4. **Forensics without restore**: `POST /v1/state_at {"when":
   "2026-06-01T00:00:00"}` — bi-temporal replay, no downtime.
