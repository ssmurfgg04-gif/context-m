"""Context-M REST API server — dependency-free, OpenAPI-documented.

Enterprises integrate over HTTP, not Python. This server exposes the
full Memory fabric over a Mem0-compatible REST surface with:

  * Bearer API-key auth (RBAC: admin / operator / reader / auditor)
  * token-bucket rate limiting per key
  * hash-chained audit logging of every request
  * Prometheus metrics at /metrics, liveness at /healthz, readiness
    with a real probe at /readyz
  * OpenAPI 3.1 spec served live at /openapi.json
  * governance endpoints: snapshot, restore, erase (GDPR), PITR
  * NSR-inspired swappable decoder surface: /v1/export?format=rdf|
    json|datalog|llm_prompt — same palace + Trace, different output
  * Aeon-inspired typed-edge + consolidate + chaos-ingest surface:
    /v1/consolidate (POST, admin) — triggers dreaming + lifecycle
    /v1/chaos       (POST, admin/operator) — zero-config auto-ingest
    /v1/sparql      (GET/POST, reader+) — inline SPARQL endpoint
  * Optional co-hosted SPARQL endpoint via `--sparql-port N`: shares
    one Memory instance with the REST API so external graph tools
    (Apache Jena, BlazeGraph, rdflib) can query Context-M directly.
  * SIGTERM/SIGINT graceful shutdown — in-flight requests drain.

Zero third-party dependencies — stdlib ``http.server`` with a thread
pool, exactly like the MCP server (edge-deployable, μ=0 intact).

Run:
    python -m context_m.server.rest --db /data/mem.db --port 8900
    python -m context_m.server.rest --sparql-port 8910   # co-hosted SPARQL
    CONTEXT_M_MASTER_KEY=$(cat /data/mem.db.key) python -m context_m.server.rest
"""

from __future__ import annotations

import argparse
import json
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from context_m import metrics as core_metrics
from context_m.api.memory import Memory
from context_m.config import Config
from context_m.security.rbac import (APIKeyStore, RBACError, authorize,
                                     ROLES)
from context_m.server.metrics import REGISTRY

MAX_BODY = 8 * 1024 * 1024  # 8 MiB


# ------------------------------------------------------------------ ratelimit
class TokenBucket:
    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate
        self.burst = burst
        self._tokens: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, ts = self._tokens.get(key, [float(self.burst), now])
            refill = (now - ts) * self.rate
            tokens = min(float(self.burst), tokens + refill)
            if tokens < 1.0:
                self._tokens[key] = [tokens, now]
                return False
            self._tokens[key] = [tokens - 1.0, now]
            return True


# ------------------------------------------------------------------ per-endpoint ratelimit
# P2 #8 from code review: SPARQL queries are slower than /healthz and
# previously shared one bucket. A SPARQL client issuing SELECT queries at
# the global rate would (a) starve /healthz probes and (b) be throttled at
# the wrong RPS for graph workload. Per-endpoint buckets fix both.
#
# Tier map (rate / burst per key per tier):
#   * "fast"    : /healthz, /readyz, /metrics, /openapi.json, OPTIONS
#                 cheap probes — high RPS, low cost-of-allow
#   * "medium"  : /v1/add, /v1/search, /v1/memories*, /v1/users, /v1/stats,
#                 /v1/verify, /v1/audit, /v1/keys*, /v1/state_at,
#                 /v1/snapshot, /v1/restore, /v1/erase, /v1/retention,
#                 /v1/export, /v1/consolidate, /v1/chaos, /v1/federation/*
#                 normal REST workload
#   * "slow"    : /v1/sparql (GET+POST)
#                 SPARQL SELECT queries do graph traversal; a single query
#                 can take 50-200 ms — separate, smaller bucket so SPARQL
#                 clients cannot starve the REST surface and vice versa.
RATE_LIMIT_TIERS: dict[str, tuple[float, int]] = {
    "fast":   (200.0, 400),    # 200 rps / burst 400  — health probes
    "medium": (50.0,  100),    # 50  rps / burst 100  — REST default
    "slow":   (10.0,  20),     # 10  rps / burst 20   — SPARQL (graph traversal)
}


def _tier_for_path(path: str) -> str:
    """Classify an HTTP path into a rate-limit tier.

    Returns one of 'fast' | 'medium' | 'slow'. Defaults to 'medium' so
    any unmapped /v1/* route inherits the safe default rather than the
    fast-probe bucket (which would let an attacker bypass throttling by
    inventing routes).
    """
    p = path.split("?", 1)[0].rstrip("/") or "/"
    if p in ("/healthz", "/readyz", "/metrics", "/openapi.json", "/"):
        return "fast"
    if p == "/v1/sparql":
        return "slow"
    return "medium"


class TieredTokenBuckets:
    """Per-endpoint token buckets — one bucket per (tier, key).

    Each tier has its own rate/burst; each (tier, key) pair has its own
    running token count. This means a SPARQL client hammering /v1/sparql
    will not exhaust the budget for the same client's /v1/search calls,
    and a monitoring agent hitting /healthz every 100ms will never be
    throttled by a SPARQL DoS.
    """

    def __init__(self, tiers: dict[str, tuple[float, int]] | None = None
                 ) -> None:
        self.tiers = tiers or RATE_LIMIT_TIERS
        self._buckets: dict[tuple[str, str], TokenBucket] = {}
        self._lock = threading.Lock()

    def _bucket(self, tier: str, key: str) -> TokenBucket:
        bk = (tier, key)
        b = self._buckets.get(bk)
        if b is not None:
            return b
        with self._lock:
            b = self._buckets.get(bk)
            if b is None:
                rate, burst = self.tiers.get(tier, self.tiers["medium"])
                b = TokenBucket(rate, burst)
                self._buckets[bk] = b
            return b

    def allow(self, tier: str, key: str) -> bool:
        return self._bucket(tier, key).allow(key)


# ------------------------------------------------------------------ openapi
def openapi_spec() -> dict:
    def op(summary: str, params: list | None = None, body: bool = False,
           roles: list[str] | None = None, responses=None) -> dict:
        d: dict = {"summary": summary, "responses": responses or
                   {"200": {"description": "OK"},
                    "401": {"description": "invalid or missing API key"},
                    "403": {"description": "role not permitted"},
                    "429": {"description": "rate limited"}}}
        tag = {"$ref": "#/components/securitySchemes/bearer"}
        if roles:
            d["description"] = f"roles: {', '.join(roles)}"
        d["security"] = [tag]
        if body:
            d["requestBody"] = {"required": True, "content": {
                "application/json": {"schema": {"type": "object"}}}}
        return d

    paths = {
        "/healthz": {"get": {"summary": "Liveness probe (no auth)",
                              "security": [], "responses": {
                                  "200": {"description": "alive"}}}},
        "/readyz": {"get": {"summary": "Readiness probe (real store ping)",
                             "security": [],
                             "responses": {"200": {"description": "ready"},
                                           "503": {"description": "not ready"}}}},
        "/metrics": {"get": {"summary": "Prometheus metrics (no auth)",
                              "security": []}},
        "/openapi.json": {"get": {"summary": "This document", "security": []}},
        "/v1/add": {"post": op("Ingest messages (mu=0, Mem0-compatible)",
                                body=True, roles=["admin", "operator"])},
        "/v1/search": {"post": op("Neuro-symbolic retrieval with provenance",
                                   body=True,
                                   roles=["admin", "operator", "reader"])},
        "/v1/memories": {"get": op("List memories (Mem0 get_all)",
                                    roles=["admin", "operator", "reader"])},
        "/v1/memories/{id}": {"get": op("Get one memory with source"),
                              "delete": op("Delete one memory",
                                           roles=["admin", "operator"])},
        "/v1/memories/{id}/history": {"get": op("Bi-temporal history chain")},
        "/v1/users": {"get": op("Known user scopes",
                                 roles=["admin", "operator", "reader"])},
        "/v1/stats": {"get": op("Fabric statistics")},
        "/v1/verify": {"get": op("Integrity + audit chain verification",
                                  roles=["admin", "operator", "reader",
                                         "auditor"])},
        "/v1/audit": {"get": op("Audit log tail (admin/auditor)",
                                 roles=["admin", "auditor"])},
        "/v1/keys": {"post": op("Create API key", body=True, roles=["admin"]),
                      "get": op("List API keys", roles=["admin"])},
        "/v1/keys/{id}": {"delete": op("Revoke API key", roles=["admin"])},
        "/v1/snapshot": {"post": op("Atomic snapshot backup",
                                     body=True,
                                     roles=["admin", "operator"])},
        "/v1/restore": {"post": op("Restore from snapshot", body=True,
                                    roles=["admin"])},
        "/v1/erase": {"post": op("GDPR right-to-erasure", body=True,
                                  roles=["admin"])},
        "/v1/retention": {"post": op("Apply retention policy", body=True,
                                      roles=["admin"])},
        "/v1/state_at": {"post": op("Point-in-time recovery read", body=True,
                                     roles=["admin", "operator", "reader"])},
        "/v1/sparql": {"get": op("SPARQL SELECT (inline endpoint)",
                                  roles=["admin", "operator", "reader"]),
                       "post": op("SPARQL SELECT via POST body",
                                   body=True,
                                   roles=["admin", "operator", "reader"])},
        "/v1/export": {"get": op("Export facts via swappable decoder "
                                     "(?format=rdf|json|datalog|llm_prompt)",
                                     roles=["admin", "operator", "reader"])},
        "/v1/consolidate": {"post": op("Trigger lifecycle + dreaming pass",
                                           body=True, roles=["admin"])},
        "/v1/chaos": {"post": op("Zero-config auto-ingest (EAM chaos mode)",
                                    body=True,
                                    roles=["admin", "operator"])},
        "/v1/federation/digest": {"get": op("Local-node CRDT digest "
                                                 "(for federation sync)",
                                                 roles=["admin", "operator",
                                                        "reader", "auditor"])},
        "/v1/federation/sync": {"post": op("Accept peer digest, "
                                                "return our delta envelope",
                                                body=True,
                                                roles=["admin", "operator"])},
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": "Context-M Memory Fabric API",
                 "version": "1.0.0",
                 "description": "Universal neuro-symbolic memory with "
                                "bi-temporal provenance, RBAC, audit chain, "
                                "GDPR governance. mu=0 ingest: zero LLM calls."},
        "servers": [{"url": "/"}],
        "components": {"securitySchemes": {
            "bearer": {"type": "http", "scheme": "bearer",
                       "description": "API key: ctxm_<role>_<hex>"}}},
        "security": [{"bearer": []}],
        "paths": paths,
        "tags": [{"name": "memory"}, {"name": "governance"},
                 {"name": "ops"}],
    }


# ------------------------------------------------------------------ handler
class FabricState:
    """Shared state: one Memory instance behind a re-entrant lock."""

    def __init__(self, memory: Memory) -> None:
        self.memory = memory
        self.lock = threading.RLock()
        # P2 #8: per-endpoint rate limiting. The old single TokenBucket
        # shared one budget across /healthz (1ms probe) and /v1/sparql
        # (50-200ms graph traversal). A SPARQL DoS would starve the
        # liveness probe; a /healthz flood would starve real REST traffic.
        # TieredTokenBuckets keeps a separate bucket per (tier, key) so
        # graph clients and probe clients don't interact.
        self.bucket = TieredTokenBuckets()

    # expose key store helpers
    @property
    def keys(self) -> APIKeyStore:
        return self.memory.keys


def build_handler(state: FabricState):

    class Handler(BaseHTTPRequestHandler):
        server_version = "context-m/1.0"
        protocol_version = "HTTP/1.1"

        # -------------------------------------------------------- plumbing
        def log_message(self, fmt, *args):  # quiet access log
            pass

        def _send(self, code: int, payload, content_type="application/json"):
            body = (payload if isinstance(payload, (bytes, bytearray))
                    else json.dumps(payload, default=str).encode())
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods",
                              "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                              "Content-Type, Authorization")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_OPTIONS(self):
            # CORS preflight — return 204 with permissive headers so
            # browser-based SPARQL clients (Apache Jena fetch, rdflib,
            # custom JS dashboards) can call /v1/sparql cross-origin
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods",
                              "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                              "Content-Type, Authorization")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("body too large")
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw or b"{}")
            except json.JSONDecodeError:
                raise ValueError("invalid JSON body")

        def _auth(self, action: str, path: str = "") -> dict:
            hdr = self.headers.get("Authorization") or ""
            if not hdr.startswith("Bearer "):
                REGISTRY.inc("contextm_http_requests_total",
                             {"code": "401"})
                self._send(401, {"error": "missing bearer token"})
                return {}
            key = hdr[7:].strip()
            meta = state.keys.verify(key)
            if meta is None:
                state.memory.audit_log.log("auth.failure",
                                       actor=key[:16] + "…",
                                       outcome="invalid_key")
                REGISTRY.inc("contextm_http_requests_total",
                             {"code": "401"})
                self._send(401, {"error": "invalid or revoked key"})
                return {}
            # P2 #8: per-endpoint rate limit. /healthz = 'fast' tier,
            # /v1/sparql = 'slow' tier, everything else = 'medium'.
            tier = _tier_for_path(path) if path else _tier_for_path(
                self.path.split("?")[0])
            if not state.bucket.allow(tier, key):
                REGISTRY.inc("contextm_http_requests_total",
                             {"code": "429"})
                self._send(429, {"error": f"rate limit exceeded "
                                          f"(tier={tier})"})
                return {}
            try:
                authorize(meta, action)
            except RBACError as e:
                state.memory.audit_log.log(action, actor=meta.get("label")
                                       or meta.get("id", "key"),
                                       role=meta.get("role"),
                                       outcome="denied")
                REGISTRY.inc("contextm_http_requests_total",
                             {"code": "403"})
                self._send(403, {"error": str(e)})
                return {}
            REGISTRY.inc("contextm_http_requests_total", {"code": "200"})
            return meta

        # -------------------------------------------------------- routing
        def do_GET(self):
            path = self.path.split("?")[0].rstrip("/") or "/"
            if path == "/healthz":
                return self._send(200, {"status": "alive"})
            if path == "/readyz":
                try:
                    with state.lock:
                        state.memory.store.conn.execute("SELECT 1")
                    return self._send(200, {"status": "ready"})
                except Exception:
                    return self._send(503, {"status": "not ready"})
            if path == "/metrics":
                REGISTRY.gauge("contextm_uptime_seconds",
                               time.monotonic())
                return self._send(200, REGISTRY.render(),
                                  content_type="text/plain; version=0.0.4")
            if path == "/openapi.json":
                return self._send(200, openapi_spec())
            if path == "/v1/memories":
                meta = self._auth("memory.get_all")
                if not meta:
                    return
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                with state.lock:
                    out = state.memory.get_all(
                        user_id=(q.get("user_id") or [None])[0],
                        limit=int((q.get("limit") or [200])[0]))
                return self._send(200, out)
            if path.startswith("/v1/memories/"):
                parts = path.split("/")
                mid = parts[3]
                if len(parts) >= 5 and parts[4] == "history":
                    meta = self._auth("memory.history")
                    if not meta:
                        return
                    with state.lock:
                        return self._send(200, state.memory.history(mid))
                meta = self._auth("memory.get")
                if not meta:
                    return
                with state.lock:
                    got = state.memory.get(mid)
                return self._send(200, got or {"error": "not found"},
                                  ) if got else self._send(404, {"error": "not found"})
            if path == "/v1/users":
                meta = self._auth("memory.get_all")
                if not meta:
                    return
                with state.lock:
                    return self._send(200, {"users":
                                            state.memory.users()})
            if path == "/v1/stats":
                meta = self._auth("memory.stats")
                if not meta:
                    return
                with state.lock:
                    return self._send(200, state.memory.stats())
            if path == "/v1/verify":
                meta = self._auth("memory.verify")
                if not meta:
                    return
                with state.lock:
                    out = state.memory.verify_integrity()
                    out["audit_chain"] = state.memory.audit_log.verify()
                return self._send(200, out)
            if path == "/v1/audit":
                meta = self._auth("audit.read")
                if not meta:
                    return
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                with state.lock:
                    rows = state.memory.audit_log.tail(
                        n=int((q.get("n") or [50])[0]),
                        actor=(q.get("actor") or [None])[0],
                        action=(q.get("action") or [None])[0])
                return self._send(200, {"events": rows})
            if path == "/v1/keys":
                meta = self._auth("keys.list")
                if not meta:
                    return
                with state.lock:
                    return self._send(200, {"keys": state.keys.list_keys()})
            # GET /v1/sparql?query=SELECT... — inline SPARQL endpoint
            if path == "/v1/sparql":
                meta = self._auth("sparql.query")
                if not meta:
                    return
                actor = meta.get("label") or meta.get("id", "key")
                out = self._h_sparql({}, actor, meta)
                return self._send(200, out)
            # GET /v1/export?format=rdf|json|datalog|llm_prompt
            if path == "/v1/export":
                meta = self._auth("memory.export")
                if not meta:
                    return
                actor = meta.get("label") or meta.get("id", "key")
                out = self._h_export({}, actor, meta)
                return self._send(200, out)
            # GET /v1/federation/digest — local-node CRDT digest
            if path == "/v1/federation/digest":
                meta = self._auth("federation.digest")
                if not meta:
                    return
                actor = meta.get("label") or meta.get("id", "key")
                with state.lock:
                    out = self._h_federation_digest({}, actor, meta)
                return self._send(200, out)
            return self._send(404, {"error": f"no route {path}"})

        def do_POST(self):
            path = self.path.split("?")[0].rstrip("/")
            t0 = time.monotonic()
            try:
                body = self._body()
            except ValueError as e:
                return self._send(400, {"error": str(e)})

            routes = {
                "/v1/add": ("memory.add", self._h_add),
                "/v1/search": ("memory.search", self._h_search),
                "/v1/keys": ("keys.create", self._h_key_create),
                "/v1/snapshot": ("governance.snapshot", self._h_snapshot),
                "/v1/restore": ("governance.restore", self._h_restore),
                "/v1/erase": ("governance.erase", self._h_erase),
                "/v1/retention": ("governance.retention", self._h_retention),
                "/v1/state_at": ("governance.pitr", self._h_state_at),
                "/v1/sparql": ("sparql.query", self._h_sparql),
                "/v1/export": ("memory.export", self._h_export),
                "/v1/consolidate": ("governance.consolidate",
                                       self._h_consolidate),
                "/v1/chaos": ("memory.chaos_ingest", self._h_chaos),
                "/v1/federation/sync": ("federation.sync",
                                          self._h_federation_sync),
            }
            if path not in routes:
                return self._send(404, {"error": f"no route {path}"})
            action, fn = routes[path]
            meta = self._auth(action)
            if not meta:
                return
            actor = meta.get("label") or meta.get("id", "key")
            try:
                out = fn(body, actor, meta)
                REGISTRY.observe("contextm_http_request_seconds",
                                 time.monotonic() - t0)
                return self._send(200, out)
            except RBACError as e:
                return self._send(403, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                REGISTRY.inc("contextm_http_requests_total",
                             {"code": "500"})
                state.memory.audit_log.log(action, actor=actor,
                                       role=meta.get("role"),
                                       outcome="error",
                                       meta={"error": str(e)[:200]})
                return self._send(500, {"error": str(e)[:500]})

        def do_DELETE(self):
            path = self.path.split("?")[0].rstrip("/")
            parts = path.split("/")
            if path.startswith("/v1/memories/") and len(parts) == 4:
                meta = self._auth("memory.delete")
                if not meta:
                    return
                with state.lock:
                    out = state.memory.delete(parts[3])
                return self._send(200, out)
            if path.startswith("/v1/keys/") and len(parts) == 4:
                meta = self._auth("keys.revoke")
                if not meta:
                    return
                with state.lock:
                    ok = state.keys.revoke(parts[3])
                state.memory.audit_log.log("keys.revoke",
                                       actor=meta.get("label") or "admin",
                                       resource=parts[3],
                                       outcome="revoked" if ok else "missing")
                return self._send(200, {"revoked": ok})
            return self._send(404, {"error": f"no route {path}"})

        # -------------------------------------------------------- handlers
        def _h_add(self, body, actor, meta):
            with state.lock:
                out = state.memory.add(
                    body.get("messages", body.get("text", "")),
                    user_id=body.get("user_id"),
                    agent_id=body.get("agent_id"),
                    run_id=body.get("run_id"),
                    metadata=body.get("metadata"),
                    timestamp=body.get("timestamp"))
            state.memory.audit_log.log(
                "memory.add", actor=actor, role=meta.get("role"),
                resource=body.get("user_id") or "default",
                meta={"facts": len(out.get("results", []))})
            return out

        def _h_search(self, body, actor, meta):
            with state.lock:
                out = state.memory.search(
                    body.get("query", ""),
                    user_id=body.get("user_id"),
                    limit=body.get("limit") or body.get("k"))
            state.memory.audit_log.log(
                "memory.search", actor=actor, role=meta.get("role"),
                resource=body.get("user_id") or "default",
                meta={"intent": out.get("intent")})
            return out

        def _h_key_create(self, body, actor, meta):
            role = body.get("role", "reader")
            if role not in ROLES:
                return {"error": f"role must be one of {ROLES}"}
            with state.lock:
                out = state.keys.create(role, label=body.get("label", ""),
                                        actor=actor,
                                        ttl_seconds=body.get("ttl_seconds"))
            state.memory.audit_log.log("keys.create", actor=actor,
                                   resource=out["id"],
                                   meta={"role": role})
            return out

        def _h_snapshot(self, body, actor, meta):
            path = body.get("path")
            if not path:
                return {"error": "path required"}
            with state.lock:
                return state.memory.governance.snapshot(path)

        def _h_restore(self, body, actor, meta):
            path = body.get("path")
            if not path:
                return {"error": "path required"}
            with state.lock:
                return state.memory.governance.restore(path)

        def _h_erase(self, body, actor, meta):
            uid = body.get("user_id")
            if not uid:
                return {"error": "user_id required"}
            with state.lock:
                return state.memory.governance.erase_user(
                    uid, crypto_shred=bool(body.get("crypto_shred", True)))

        def _h_retention(self, body, actor, meta):
            days = int(body.get("days", 0))
            with state.lock:
                return state.memory.governance.apply_retention(
                    days, user_id=body.get("user_id"),
                    dry_run=bool(body.get("dry_run", False)))

        def _h_state_at(self, body, actor, meta):
            when = body.get("when")
            if not when:
                return {"error": "when required (ISO datetime)"}
            with state.lock:
                rows = state.memory.governance.state_at(
                    when, user_id=body.get("user_id"))
            return {"facts": rows}

        # ---------------------------------------- NSR / Aeon / EAM surface
        def _h_sparql(self, body, actor, meta):
            """Inline SPARQL endpoint — auth'd, shares one Memory.

            GET  /v1/sparql?query=SELECT...
            POST /v1/sparql  {"query": "SELECT ..."}
            """
            # body is {} for GET — parse query string instead
            query = (body.get("query") if body else None) or ""
            if not query:
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                query = q.get("query", [""])[0]
            if not query:
                return {"error": "missing 'query' parameter"}
            # guard against giant queries (DoS protection)
            from context_m.server.sparql import MAX_QUERY_BYTES
            if len(query) > MAX_QUERY_BYTES:
                return {"error": f"query exceeds {MAX_QUERY_BYTES} bytes"}
            try:
                from context_m.server.sparql import (
                    execute_sparql, edge_triples)
                with state.lock:
                    fact_objs = state.memory.store.query_facts(
                        active=True,
                        user_id=body.get("user_id") if body else None)
                    # 4-tuple form so the blob resolver can dereference
                    # arena-stored source text via fact.source_id
                    facts = [(f.subject, f.relation, f.value,
                              f.source_id)
                             for f in fact_objs]
                    edges = edge_triples(state.memory,
                                          user_id=body.get("user_id")
                                          if body else None)
                    arena = getattr(state.memory, "blob_arena", None)
                    blob_resolver = None
                    if arena is not None:
                        def _resolve(_s, _r, source_id):
                            from context_m.trace.blob_arena import \
                                get_chunk_text
                            if not source_id:
                                return ""
                            return get_chunk_text(state.memory.store,
                                                   arena, source_id)
                        blob_resolver = _resolve
                out = execute_sparql(query, facts,
                                       user_id=body.get("user_id")
                                       if body else None,
                                       edge_triples=edges,
                                       blob_resolver=blob_resolver)
                state.memory.audit_log.log(
                    "sparql.query", actor=actor, role=meta.get("role"),
                    meta={"n_results": out.get("n_results", 0),
                           "query_head": query[:80]})
                return out
            except ValueError as e:
                return {"error": str(e)}
            except Exception as e:  # noqa: BLE001
                return {"error": f"server: {e}"}

        def _h_export(self, body, actor, meta):
            """Export facts via the swappable decoder.

            GET /v1/export?format=rdf|json|datalog|llm_prompt&user_id=X
            """
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            fmt = (q.get("format") or ["llm_prompt"])[0]
            user_id = (q.get("user_id") or [None])[0]
            from context_m.bridge.decoders import get_decoder
            try:
                decoder = get_decoder(fmt)
            except ValueError as e:
                return {"error": str(e)}
            with state.lock:
                facts = state.memory.store.query_facts(
                    active=True, user_id=user_id)
                # score by recency for export ordering
                scores = {f.id: 1.0 for f in facts}
                out = decoder.render(
                    query="", intent="export", facts=list(facts),
                    scores=scores, notes=None,
                    store=state.memory.store)
            state.memory.audit_log.log(
                "memory.export", actor=actor, role=meta.get("role"),
                resource=user_id or "default",
                meta={"format": fmt, "n_facts": len(facts)})
            # for json decoder, return parsed; otherwise return text
            if fmt == "json":
                try:
                    return json.loads(out)
                except json.JSONDecodeError:
                    pass
            return {"format": fmt, "n_facts": len(facts),
                    "content": out}

        def _h_consolidate(self, body, actor, meta):
            """Trigger the consolidate() dreaming + lifecycle pass.

            POST /v1/consolidate {"dry_run": false, "lifecycle": true,
                                  "dreaming": true, "user_id": null}
            """
            with state.lock:
                out = state.memory.consolidate(
                    dry_run=bool(body.get("dry_run", False)),
                    lifecycle=bool(body.get("lifecycle", True)),
                    dreaming=bool(body.get("dreaming", True)),
                    user_id=body.get("user_id"))
            state.memory.audit_log.log(
                "governance.consolidate", actor=actor,
                role=meta.get("role"),
                meta={"dry_run": bool(body.get("dry_run", False)),
                       "lifecycle": out.get("lifecycle", {}),
                       "dreaming": out.get("dreaming", {})})
            return out

        def _h_chaos(self, body, actor, meta):
            """Zero-config auto-ingest (EAM chaos mode).

            POST /v1/chaos {"texts": ["...", ...], "user_id": "default"}
            POST /v1/chaos {"text": "...", "user_id": "default"}
            """
            from context_m.api.chaos import chaos_ingest
            texts = body.get("texts")
            if texts is None:
                texts = [body.get("text", "")] if body.get("text") else []
            if not texts:
                return {"error": "missing 'text' or 'texts' field"}
            user_id = body.get("user_id", "default")
            with state.lock:
                out = chaos_ingest(state.memory, texts, user_id=user_id,
                                   agent_id=body.get("agent_id"),
                                   run_id=body.get("run_id"))
            state.memory.audit_log.log(
                "memory.chaos_ingest", actor=actor, role=meta.get("role"),
                resource=user_id,
                meta={"n_facts": out.get("stats", {}).get(
                    "facts_inserted", 0)})
            return out

        # ---------------------------------------- federation surface
        def _h_federation_digest(self, body, actor, meta):
            """Build a local-node digest envelope for federation sync.

            The caller (a peer FederationNode) sends our digest to
            their node, which compares against their own state and
            returns a delta envelope. We then POST that delta to
            /v1/federation/sync (below) to apply it.

            Requires a `node_id` (default: hostname) and a federation
            `key` (HMAC-SHA256 signing key — read from
            CONTEXT_M_FEDERATION_KEY env var).
            """
            import os
            import socket
            from context_m.federation.node import FederationNode
            from context_m.federation.fabric import node_from_store
            node_id = body.get("node_id") or "ctxm-" + socket.gethostname()
            fed_key = body.get("key") or os.environ.get(
                "CONTEXT_M_FEDERATION_KEY", "default-federation-key")
            with state.lock:
                node = node_from_store(node_id, state.memory.store,
                                        members=[node_id],
                                        federation_key=fed_key)
                from context_m.federation.fabric import export_to_crdt
                export_to_crdt(state.memory.store, node,
                               user_id=body.get("user_id"))
                env = node.digest_envelope()
            state.memory.audit_log.log(
                "federation.digest", actor=actor, role=meta.get("role"),
                meta={"node_id": node_id})
            return env

        def _h_federation_sync(self, body, actor, meta):
            """Accept a peer's digest envelope, return our delta.

            POST /v1/federation/sync {<peer digest envelope>}
            -> {<our delta envelope>}

            The delta contains the facts the peer is missing. The peer
            then POSTs that delta to its own /v1/federation/apply (TODO)
            or to a future /v1/federation/apply endpoint here, which calls
            node.apply_delta_envelope + apply_to_store.
            """
            import os
            import socket
            from context_m.federation.node import FederationNode
            from context_m.federation.fabric import (node_from_store,
                                                       export_to_crdt)
            node_id = body.get("node_id") or "ctxm-" + socket.gethostname()
            fed_key = body.get("key") or os.environ.get(
                "CONTEXT_M_FEDERATION_KEY", "default-federation-key")
            peer_env = body.get("peer_envelope") or body
            with state.lock:
                node = node_from_store(node_id, state.memory.store,
                                        members=[node_id],
                                        federation_key=fed_key)
                export_to_crdt(state.memory.store, node,
                               user_id=body.get("user_id"))
                delta_env = node.delta_envelope_for(peer_env)
            state.memory.audit_log.log(
                "federation.sync", actor=actor, role=meta.get("role"),
                meta={"node_id": node_id,
                       "peer": peer_env.get("from", "?")})
            return delta_env

    return Handler


# ------------------------------------------------------------------ launch
def serve(memory: Memory | None = None, host: str = "0.0.0.0",
          port: int = 8900, *, sparql_port: int | None = None,
          sparql_host: str = "0.0.0.0",
          sparql_user_id: str | None = None) -> ThreadingHTTPServer:
    if memory is None:
        memory = Memory(Config.from_env())
    state = FabricState(memory)
    httpd = ThreadingHTTPServer((host, port), build_handler(state))
    httpd.daemon_threads = True
    # optionally co-host a SPARQL endpoint sharing the same Memory instance
    if sparql_port:
        from context_m.server.sparql import SparqlServer
        sparql = SparqlServer(memory, host=sparql_host, port=sparql_port,
                               user_id=sparql_user_id)
        sparql.start_background()
        httpd.sparql = sparql  # type: ignore[attr-defined]
    return httpd


def main() -> None:
    ap = argparse.ArgumentParser(prog="contextm-serve",
                                 description="Context-M REST API server")
    ap.add_argument("--db", default=":memory:", help="SQLite path")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8900)
    ap.add_argument("--pii", default=None,
                    help="off|redact|block|tag (default: config/env)")
    ap.add_argument("--admin-key", default=None,
                    help="create this admin API key at boot (dev convenience)")
    ap.add_argument("--sparql-port", type=int, default=None,
                    help="co-host a SPARQL endpoint on this port "
                         "(shares one Memory instance with the REST API)")
    ap.add_argument("--sparql-host", default="0.0.0.0",
                    help="bind SPARQL endpoint to this host")
    ap.add_argument("--sparql-user-id", default=None,
                    help="scope SPARQL queries to a single user")
    args = ap.parse_args()

    cfg = Config.from_env(db_path=args.db)
    if args.pii:
        cfg.pii_mode = args.pii
    memory = Memory(cfg)
    if args.admin_key:
        meta = memory.keys.create("admin", label="boot-admin")
        print(f"[context-m] admin API key (save now, shown once): {meta['key']}")
    httpd = serve(memory, args.host, args.port,
                  sparql_port=args.sparql_port,
                  sparql_host=args.sparql_host,
                  sparql_user_id=args.sparql_user_id)
    print(f"[context-m] REST API on http://{args.host}:{args.port}"
          f"  (db={args.db}, pii={cfg.pii_mode}, "
          f"encrypt={cfg.encryption_at_rest})")
    if args.sparql_port:
        print(f"[context-m] SPARQL endpoint on http://{args.sparql_host}:"
              f"{args.sparql_port}/  (co-hosted, shares Memory)")

    # graceful shutdown on SIGTERM/SIGINT — drain in-flight requests.
    # IMPORTANT: signal handlers run on the MAIN thread, but
    # `httpd.shutdown()` blocks on `__is_shut_down` which is only
    # set when serve_forever() exits its loop. So we MUST run
    # serve_forever() in a daemon thread and let the main thread
    # block on a sentinel Event instead. SIGTERM sets the event,
    # main thread wakes up, calls shutdown() on the daemon-thread
    # serve loop, then exits cleanly. This avoids the classic
    # self-deadlock where signal→shutdown() blocks on the same
    # thread that's supposed to exit serve_forever().
    stop_event = threading.Event()

    def _shutdown(signum, frame):
        print(f"\n[context-m] received signal {signum} — shutting down...")
        stop_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # run serve_forever() in a daemon thread; main thread waits on
    # the event so signal handlers can fire cleanly.
    server_thread = threading.Thread(
        target=httpd.serve_forever, daemon=True,
        name="contextm-rest")
    server_thread.start()
    print(f"[context-m] ready — press Ctrl+C to shut down")
    try:
        # block until shutdown signal sets the event
        while not stop_event.is_set():
            stop_event.wait(timeout=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        sparql = getattr(httpd, "sparql", None)
        if sparql is not None:
            sparql.stop()
        # shutdown() is now safe — runs in main thread, server loop
        # is in a different daemon thread
        httpd.shutdown()
        httpd.server_close()
        memory.close()


if __name__ == "__main__":
    main()
