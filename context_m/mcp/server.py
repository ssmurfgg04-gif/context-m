"""Model Context Protocol server — stdio JSON-RPC 2.0.

Day-1 MCP support per the plan: any agent (Claude, Cursor, OpenCode)
discovers Context-M as a native memory tool. Zero dependencies — the
protocol is implemented directly on line-delimited JSON-RPC.

Run:  cortexm serve          (or: python -m context_m.mcp.server)
Config: CONTEXT_M_DB sets the database path; CONTEXT_M_CODEC selects
the storage tier (int8 | binary | rabitq | pq).
"""

from __future__ import annotations

import json
import os
import sys

from context_m.config import Config
from context_m.api.memory import Memory

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "context-m", "version": "0.1.0"}

TOOLS = [
    {
        "name": "contextm_add",
        "description": "Store memories from a conversation (μ=0: deterministic, "
                       "no LLM calls). Accepts a message string or a list of "
                       "{role, content} dicts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "messages": {"type": ["string", "array"]},
                "user_id": {"type": "string", "default": "default"},
                "agent_id": {"type": "string"},
                "run_id": {"type": "string"},
                "timestamp": {"type": "string"},
            },
            "required": ["messages"],
        },
    },
    {
        "name": "contextm_search",
        "description": "Neuro-symbolic memory retrieval with cryptographic "
                       "provenance (query → VSA match → symbolic dereference → "
                       "source hash → original text).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "user_id": {"type": "string", "default": "default"},
                "limit": {"type": "integer", "default": 12},
            },
            "required": ["query"],
        },
    },
    {
        "name": "contextm_get_all",
        "description": "List all active memories for a user.",
        "inputSchema": {
            "type": "object",
            "properties": {"user_id": {"type": "string"},
                           "limit": {"type": "integer", "default": 200}},
        },
    },
    {
        "name": "contextm_history",
        "description": "Bi-temporal history of a fact chain, including all "
                       "supersessions.",
        "inputSchema": {
            "type": "object",
            "properties": {"memory_id": {"type": "string"}},
            "required": ["memory_id"],
        },
    },
    {
        "name": "contextm_temporal",
        "description": "Zep-compatible temporal query: facts in a time window. "
                       "op: before | after | between; field: valid (reality) "
                       "or tx (when recorded).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["before", "after", "between"]},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "user_id": {"type": "string"},
                "field": {"type": "string", "default": "valid"},
            },
            "required": ["op"],
        },
    },
    {
        "name": "contextm_audit",
        "description": "The 'Why' audit trail for a query: full provenance "
                       "chain with hash verification for every returned fact.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"},
                           "user_id": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "contextm_prove",
        "description": "Zero-knowledge-lite proof: prove a matching fact "
                       "exists (Merkle membership + attestation) without "
                       "revealing its content to the LLM.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"},
                           "user_id": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "contextm_stats",
        "description": "Memory fabric statistics: facts, vectors, codec, "
                       "SLB hit rate, μ=0 protocol status.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "contextm_delete",
        "description": "Deactivate a memory (audit-logged, never hard-deleted).",
        "inputSchema": {
            "type": "object",
            "properties": {"memory_id": {"type": "string"}},
            "required": ["memory_id"],
        },
    },
]


class MCPServer:
    def __init__(self, memory: Memory) -> None:
        self.memory = memory

    # ------------------------------------------------------------------
    def handle(self, request: dict) -> dict | None:
        method = request.get("method", "")
        req_id = request.get("id")
        if method == "initialize":
            return self._ok(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            })
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return self._ok(req_id, {})
        if method == "tools/list":
            return self._ok(req_id, {"tools": TOOLS})
        if method == "tools/call":
            return self._ok(req_id, self._call_tool(
                request.get("params", {}).get("name", ""),
                request.get("params", {}).get("arguments", {}) or {}))
        if method == "resources/list":
            return self._ok(req_id, {"resources": []})
        if req_id is not None:
            return self._err(req_id, -32601, f"method not found: {method}")
        return None

    # ------------------------------------------------------------------
    def _call_tool(self, name: str, args: dict) -> dict:
        m = self.memory
        try:
            if name == "contextm_add":
                out = m.add(args.get("messages"),
                            user_id=args.get("user_id", "default"),
                            agent_id=args.get("agent_id"),
                            run_id=args.get("run_id"),
                            timestamp=args.get("timestamp"))
                text = json.dumps({"stored": len(out.get("results", [])),
                                   "stats": out.get("stats")}, default=str)
            elif name == "contextm_search":
                out = m.search(args.get("query", ""),
                               user_id=args.get("user_id", "default"),
                               limit=args.get("limit", 12))
                text = out["context_block"]
            elif name == "contextm_get_all":
                out = m.get_all(user_id=args.get("user_id", "default"),
                                limit=args.get("limit", 200))
                text = "\n".join(r["memory"] for r in out["results"]) or "(empty)"
            elif name == "contextm_history":
                out = m.history(args.get("memory_id", ""))
                text = json.dumps(out, indent=1, default=str)
            elif name == "contextm_temporal":
                op = args.get("op", "between")
                if op == "before":
                    out = m.get_before(args.get("end") or args.get("start"),
                                       user_id=args.get("user_id", "default"),
                                       field=args.get("field", "valid"))
                elif op == "after":
                    out = m.get_after(args.get("start"),
                                      user_id=args.get("user_id", "default"),
                                      field=args.get("field", "valid"))
                else:
                    out = m.get_between(args.get("start"), args.get("end"),
                                        user_id=args.get("user_id", "default"),
                                        field=args.get("field", "valid"))
                text = json.dumps(out, indent=1, default=str)
            elif name == "contextm_audit":
                out = m.audit(args.get("query", ""),
                              user_id=args.get("user_id", "default"))
                text = json.dumps(out, indent=1, default=str)
            elif name == "contextm_prove":
                proof = m.prove(args.get("query", ""),
                                user_id=args.get("user_id", "default"))
                text = proof["llm_view"] + f"\nMerkle root: {proof['merkle_root'][:16]}…"
            elif name == "contextm_stats":
                text = json.dumps(m.stats(), indent=1, default=str)
            elif name == "contextm_delete":
                out = m.delete(args.get("memory_id", ""))
                text = json.dumps(out)
            else:
                return {"content": [{"type": "text",
                                     "text": f"unknown tool {name}"}],
                        "isError": True}
            return {"content": [{"type": "text", "text": text}]}
        except Exception as e:  # surface errors to the agent
            return {"content": [{"type": "text", "text": f"error: {e}"}],
                    "isError": True}

    # ------------------------------------------------------------------
    @staticmethod
    def _ok(req_id, result) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def _err(req_id, code, message) -> dict:
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": code, "message": message}}


def serve(db_path: str | None = None) -> None:
    cfg = Config.from_env()
    if db_path:
        cfg.db_path = db_path
    memory = Memory(cfg)
    server = MCPServer(memory)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = server.handle(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    serve()
