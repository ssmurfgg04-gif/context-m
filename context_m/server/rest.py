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

Zero third-party dependencies — stdlib ``http.server`` with a thread
pool, exactly like the MCP server (edge-deployable, μ=0 intact).

Run:
    python -m context_m.server.rest --db /data/mem.db --port 8900
    CONTEXT_M_MASTER_KEY=$(cat /data/mem.db.key) python -m context_m.server.rest
"""

from __future__ import annotations

import argparse
import json
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
        self.bucket = TokenBucket(memory.config.rate_limit_rps,
                                  memory.config.rate_limit_burst)

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
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

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

        def _auth(self, action: str) -> dict:
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
            if not state.bucket.allow(key):
                REGISTRY.inc("contextm_http_requests_total",
                             {"code": "429"})
                self._send(429, {"error": "rate limit exceeded"})
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

    return Handler


# ------------------------------------------------------------------ launch
def serve(memory: Memory | None = None, host: str = "0.0.0.0",
          port: int = 8900) -> ThreadingHTTPServer:
    if memory is None:
        memory = Memory(Config.from_env())
    state = FabricState(memory)
    httpd = ThreadingHTTPServer((host, port), build_handler(state))
    httpd.daemon_threads = True
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
    args = ap.parse_args()

    cfg = Config.from_env(db_path=args.db)
    if args.pii:
        cfg.pii_mode = args.pii
    memory = Memory(cfg)
    if args.admin_key:
        import secrets
        role = "admin"
        meta = memory.keys.create(role, label="boot-admin")
        print(f"[context-m] admin API key (save now, shown once): {meta['key']}")
    httpd = serve(memory, args.host, args.port)
    print(f"[context-m] REST API on http://{args.host}:{args.port}"
          f"  (db={args.db}, pii={cfg.pii_mode}, "
          f"encrypt={cfg.encryption_at_rest})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        memory.close()


if __name__ == "__main__":
    main()
