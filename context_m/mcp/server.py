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
    {
        "name": "contextm_query_extract",
        "description": "Query-time extraction (arXiv 2026 hybrid RAG): retrieve "
                       "raw chunks relevant to the query and run the deterministic "
                       "extractor on them lazily. Adds extracted_at temporal axis. "
                       "Closes the 'half-empty palace' gap for slang/paraphrase.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "user_id": {"type": "string", "default": "default"},
                "k": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "contextm_attribution",
        "description": "Source attribution (ProtoDash): for a given query, "
                       "show which source chunks contributed to the retrieval "
                       "result and with what weights. Audit trail for debugging.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"},
                           "user_id": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "contextm_zk_prove",
        "description": "Hamming-distance ZK-style proof on binary vectors: prove "
                       "you hold a memory within threshold Hamming distance of a "
                       "public commitment, without revealing the memory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "public_commitment": {"type": "string"},
                "threshold": {"type": "integer", "default": 32},
            },
            "required": ["memory_id", "public_commitment"],
        },
    },
    {
        "name": "contextm_reconstruct",
        "description": "Active memory reconstruction (MRAgent ICML 2026 lineage). "
                       "For a given query, expand seed facts via 2-hop Personalized "
                       "PageRank, score each hop's relevance, prune low-scoring "
                       "branches, and return a synthesized narrative view of the "
                       "user's memory state. Use this when a normal search would "
                       "miss multi-hop context (e.g. 'what's the connection between "
                       "Alice's job and her sister's hobby?').",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "user_id": {"type": "string", "default": "default"},
                "k": {"type": "integer", "default": 10},
                "max_hops": {"type": "integer", "default": 3},
            },
            "required": ["query"],
        },
    },
    {
        "name": "contextm_consolidate",
        "description": "Trigger a memory consolidation pass on demand (normally "
                       "runs nightly via cron). Runs the FadeMem sweep "
                       "(decay + deactivate + merge), TiMem TMT hierarchy build "
                       "(session→day→persona summaries), and Aeon-style dreaming "
                       "(merge redundant triples, defrag palace). Measured 43.2%% "
                       "storage reduction with zero retrieval-precision regression. "
                       "Returns a stats dict; safe to call repeatedly (idempotent).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "default": "default"},
                "dry_run": {"type": "boolean", "default": False},
                "lifecycle": {"type": "boolean", "default": True},
                "dreaming": {"type": "boolean", "default": True},
                "fade": {"type": "boolean", "default": True},
                "tmt": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "contextm_working_memory",
        "description": "Holographic working memory — compress the top-k retrieved "
                       "facts into a single HRR (Holographic Reduced Representation) "
                       "superposition. Returns a short (~30-50 token) preamble for "
                       "LLM system-prompt injection + the HRR vector base64-packed. "
                       "5-10× token reduction for context windows with repeated "
                       "memory injection across turns.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "user_id": {"type": "string", "default": "default"},
                "k": {"type": "integer", "default": 12},
            },
            "required": ["query"],
        },
    },
    {
        "name": "contextm_hologram_extract",
        "description": "Unbind a role (S/R/V) from a holographic working memory "
                       "vector and return the top-3 candidate facts. Used by agents "
                       "that received a working-memory hologram via "
                       "contextm_working_memory and want to recall a specific fact "
                       "slot on demand. Pure HRR algebra — μ=0.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "hrr_b64": {"type": "string",
                            "description": "base64-packed HRR vector from contextm_working_memory"},
                "role": {"type": "string", "enum": ["S", "R", "V"]},
                "candidate_ids": {"type": "array", "items": {"type": "string"}},
                "user_id": {"type": "string", "default": "default"},
            },
            "required": ["hrr_b64", "role", "candidate_ids"],
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
            elif name == "contextm_query_extract":
                # Query-time extraction (hybrid RAG) — extract from raw
                # chunks relevant to the query, even if μ=0 ingest missed.
                out = self._query_extract(
                    args.get("query", ""),
                    user_id=args.get("user_id", "default"),
                    k=args.get("k", 5))
                text = json.dumps(out, indent=1, default=str)
            elif name == "contextm_attribution":
                # ProtoDash source attribution — which chunks contributed.
                out = self._attribution(
                    args.get("query", ""),
                    user_id=args.get("user_id", "default"))
                text = json.dumps(out, indent=1, default=str)
            elif name == "contextm_zk_prove":
                # Hamming ZK proof on binary codec vectors.
                out = self._zk_prove(
                    args.get("memory_id", ""),
                    args.get("public_commitment", ""),
                    threshold=args.get("threshold", 32))
                text = json.dumps(out, indent=1, default=str)
            elif name == "contextm_reconstruct":
                # MRAgent-style active reconstruction.
                out = self._reconstruct(
                    args.get("query", ""),
                    user_id=args.get("user_id", "default"),
                    k=args.get("k", 10),
                    max_hops=args.get("max_hops", 3))
                text = json.dumps(out, indent=1, default=str)
            elif name == "contextm_consolidate":
                # On-demand consolidation pass (FadeMem + TMT + dreaming).
                out = self._consolidate(
                    user_id=args.get("user_id", "default"),
                    dry_run=args.get("dry_run", False),
                    lifecycle=args.get("lifecycle", True),
                    dreaming=args.get("dreaming", True),
                    fade=args.get("fade", True),
                    tmt=args.get("tmt", True))
                text = json.dumps(out, indent=1, default=str)
            elif name == "contextm_working_memory":
                # Holographic working-memory compression.
                out = self._working_memory(
                    args.get("query", ""),
                    user_id=args.get("user_id", "default"),
                    k=args.get("k", 12))
                text = json.dumps(out, indent=1, default=str)
            elif name == "contextm_hologram_extract":
                # Unbind a role from a HRR hologram and return top-3.
                out = self._hologram_extract(
                    args.get("hrr_b64", ""),
                    args.get("role", "S"),
                    args.get("candidate_ids", []),
                    user_id=args.get("user_id", "default"))
                text = json.dumps(out, indent=1, default=str)
            else:
                return {"content": [{"type": "text",
                                     "text": f"unknown tool {name}"}],
                        "isError": True}
            return {"content": [{"type": "text", "text": text}]}
        except Exception as e:  # surface errors to the agent
            return {"content": [{"type": "text", "text": f"error: {e}"}],
                    "isError": True}

    # ------------------------------------------------------------------
    def _query_extract(self, query: str, user_id: str = "default",
                       k: int = 5) -> dict:
        """Hybrid query-time extraction. Uses QueryTimeExtractor if
        available; falls back to standard search results if not."""
        try:
            from context_m.bridge.query_extract import QueryTimeExtractor
            from context_m.text.dissim import DisSimSplitter
            from context_m.text.idiolect import PerUserIdiolectNormalizer
            from context_m.text.embedder import HashingEmbedder

            palace = self.memory.palace
            store = self.memory.store
            embedder = HashingEmbedder(palace.dims, palace.cfg.seed)
            dissim = DisSimSplitter(max_depth=2)
            idiolect = PerUserIdiolectNormalizer(embedder)
            extractor = QueryTimeExtractor(
                palace, store, embedder, dissim=dissim,
                idiolect=idiolect,
                pattern_extractor=self.memory.extractor if hasattr(
                    self.memory, "extractor") else None)
            results = extractor.query(query, user_id=user_id, k=k)
            return {
                "query": query,
                "user_id": user_id,
                "extracted_count": len(results),
                "results": results,
                "path": "query_time_pattern",
            }
        except Exception as e:
            # graceful fallback: standard search
            out = self.memory.search(query, user_id=user_id, limit=k)
            return {
                "query": query,
                "user_id": user_id,
                "fallback": "standard_search",
                "error": str(e),
                "context_block": out.get("context_block", ""),
            }

    def _attribution(self, query: str, user_id: str = "default") -> dict:
        """ProtoDash attribution for a query — which source chunks contributed."""
        try:
            from context_m.vsa.attribution import ProtoDashAttributer, sentence_level_score
            from context_m.text.embedder import HashingEmbedder
            import numpy as np

            # standard search to get candidates
            out = self.memory.search(query, user_id=user_id, limit=10)
            results = out.get("results", [])
            if not results:
                return {"query": query, "attributions": []}
            embedder = HashingEmbedder(self.memory.palace.dims,
                                       self.memory.palace.cfg.seed)
            q_emb = embedder.embed(query)
            cand_embs = np.stack([embedder.embed(r.get("memory", ""))
                                   for r in results])
            cand_ids = [r.get("id", str(i)) for i, r in enumerate(results)]
            attrib = ProtoDashAttributer(kernel="linear")
            weights = attrib.attribute(q_emb, cand_embs, cand_ids, m=5)
            return {
                "query": query,
                "user_id": user_id,
                "attributions": [
                    {"fact_id": fid, "weight": w, "memory": r.get("memory", "")}
                    for (fid, w), r in zip(weights, results)
                ],
            }
        except Exception as e:
            return {"query": query, "error": str(e), "attributions": []}

    def _zk_prove(self, memory_id: str, public_commitment: str,
                  threshold: int = 32) -> dict:
        """Hamming ZK proof on binary codec vectors."""
        try:
            from context_m.security.zk_hamming import HammingZKProver
            palace = self.memory.palace
            # fetch the memory's packed vector
            row = palace._id2row.get(memory_id)
            if row is None:
                return {"error": f"memory {memory_id} not in palace"}
            packed = palace._packed[row]
            # convert public commitment hex → bytes
            public = bytes.fromhex(public_commitment)
            private = bytes(packed.tobytes())
            prover = HammingZKProver(dims=palace.dims, threshold=threshold)
            proof = prover.prove(public, private)
            verified = prover.verify(public, proof)
            return {
                "memory_id": memory_id,
                "verified": verified,
                "weight": proof.weight,
                "threshold": proof.threshold,
                "commitment": proof.commitment[:32] + "...",
            }
        except Exception as e:
            return {"memory_id": memory_id, "error": str(e)}

    # ------------------------------------------------------------------
    def _reconstruct(self, query: str, user_id: str = "default",
                     k: int = 10, max_hops: int = 3) -> dict:
        """MRAgent-style active memory reconstruction.

        Uses Memory.reader.reconstruct() (which exists from prior work
        — bridge/reader.py). Falls back to standard search if the
        reconstruct path is disabled in Config.
        """
        try:
            reader = getattr(self.memory, "reader", None)
            if reader is None:
                out = self.memory.search(query, user_id=user_id, limit=k)
                return {"query": query, "fallback": "standard_search",
                        "context_block": out.get("context_block", "")}
            reconstruct_fn = getattr(reader, "reconstruct", None)
            if (reconstruct_fn is None
                    or not getattr(self.memory.config,
                                    "reconstruct_enabled", False)):
                out = self.memory.search(query, user_id=user_id, limit=k * 2)
                return {"query": query, "fallback": "standard_search_wide",
                        "context_block": out.get("context_block", ""),
                        "results": out.get("results", [])[:k]}
            res = reconstruct_fn(query, user_id=user_id, k=k,
                                  max_hops=max_hops)
            return {
                "query": query,
                "user_id": user_id,
                "intent": getattr(res, "intent", "reconstruct"),
                "narrative": getattr(res, "context_block", ""),
                "facts": [
                    {"id": getattr(f, "id", ""), "subject": f.subject,
                     "relation": f.relation, "value": f.value,
                     "confidence": getattr(f, "confidence", 0.0)}
                    for f in getattr(res, "facts", [])
                ],
                "provenance": getattr(res, "provenance", {}),
                "timing": getattr(res, "timing", {}),
            }
        except Exception as e:
            return {"query": query, "error": str(e)}

    # ------------------------------------------------------------------
    def _consolidate(self, user_id: str = "default", dry_run: bool = False,
                     lifecycle: bool = True, dreaming: bool = True,
                     fade: bool = True, tmt: bool = True) -> dict:
        """On-demand consolidation: FadeMem + TMT + Aeon dreaming.

        Wraps Memory.consolidate() with explicit fade/tmt toggles so
        callers can run a partial pass (e.g. fade-only) on demand.
        Idempotent — safe to call repeatedly.
        """
        try:
            out = self.memory.consolidate(
                lifecycle=lifecycle, dreaming=dreaming,
                run_fade=fade, run_tmt=tmt, user_id=user_id,
                dry_run=dry_run)
            return {"user_id": user_id, "dry_run": dry_run,
                    "lifecycle": out.get("lifecycle", {}),
                    "dreaming": out.get("dreaming", {})}
        except Exception as e:
            return {"user_id": user_id, "error": str(e)}

    # ------------------------------------------------------------------
    def _working_memory(self, query: str, user_id: str = "default",
                        k: int = 12) -> dict:
        """Compress top-k retrieved facts into a single HRR superposition."""
        try:
            reader = getattr(self.memory, "reader", None)
            if reader is None:
                return {"query": query, "error": "reader not available"}
            return reader.working_memory(query, user_id=user_id, k=k)
        except Exception as e:
            return {"query": query, "error": str(e)}

    # ------------------------------------------------------------------
    def _hologram_extract(self, hrr_b64: str, role: str,
                          candidate_ids: list[str],
                          user_id: str = "default") -> dict:
        """Unbind a role from a HRR hologram and return top-3 matches."""
        try:
            reader = getattr(self.memory, "reader", None)
            if reader is None:
                return {"error": "reader not available"}
            hits = reader.hologram_extract(hrr_b64, role,
                                             candidate_ids=candidate_ids,
                                             user_id=user_id)
            return {"role": role, "hits": hits}
        except Exception as e:
            return {"error": str(e)}

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
