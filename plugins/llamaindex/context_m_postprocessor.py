"""LlamaIndex memory postprocessor for Context-M.

LlamaIndex's NodePostprocessor interface is the canonical hook for
custom memory/RAG in their agent framework. Mem0 ships a
`MemoryPostprocessor`; Context-M needs one too.

Usage:
    from cortexm.plugins.llamaindex import ContextMMemoryPostprocessor
    from llama_index.core.postprocessor_types import NodePostprocessor
    # in a query engine pipeline:
    #   query_engine = index.as_query_engine(
    #       node_postprocessors=[ContextMMemoryPostprocessor(user_id="alice")])
    # the postprocessor:
    #   1. calls Context-M's /v1/search for the user's recent facts
    #   2. prepends the context block to the retrieved nodes' text
    #   3. the LLM sees the user's memory + the index's hits

This adapter uses urllib (no hard llama-index dependency at import
time) so the module can be vendored standalone.
"""
from __future__ import annotations

import json
import os
from typing import Any, List
from urllib import request as urlreq
from urllib.error import URLError


class ContextMMemoryPostprocessor:
    """LlamaIndex NodePostprocessor duck-type for Context-M.

    Calls /v1/search on the Context-M REST server and prepends the
    returned context block to the top retrieved node's text. The LLM
    therefore sees both the user's persistent memory AND the index's
    hits in the same prompt — no separate memory injection step.

    Configure via env:
      CONTEXT_M_REST_URL   default http://localhost:8900
      CONTEXT_M_API_KEY    default none
      CONTEXT_M_USER_ID    default "default"
    """

    def __init__(self, *,
                 rest_url: str | None = None,
                 api_key: str | None = None,
                 user_id: str = "default",
                 k: int = 12,
                 timeout: float = 5.0,
                 prepend_to_top_n: int = 1) -> None:
        self.rest_url = (rest_url or os.environ.get("CONTEXT_M_REST_URL")
                          or "http://localhost:8900").rstrip("/")
        self.api_key = (api_key or os.environ.get("CONTEXT_M_API_KEY")
                        or "")
        self.user_id = user_id
        self.k = k
        self.timeout = timeout
        self.prepend_to_top_n = prepend_to_top_n

    # ------------------------------------------------------------------
    def _post(self, path: str, body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urlreq.Request(f"{self.rest_url}{path}", data=data,
                              headers=headers, method="POST")
        try:
            with urlreq.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except URLError as e:
            return {"error": str(e), "context_block": ""}

    # ------------------------------------------------------------------
    # NodePostprocessor duck-type
    # ------------------------------------------------------------------
    def postprocess_nodes(self, nodes: List[Any],
                           query_str: str | None = None,
                           **kwargs) -> List[Any]:
        """Prepend Context-M context block to the top-N retrieved nodes.

        `nodes` is a list of objects with `.node.text` (NodeWithScore
        shape in LlamaIndex). We add the memory block as a preamble on
        the top-N nodes (default 1) so the LLM sees memory + hits.
        """
        if not nodes or not query_str:
            return nodes
        out = self._post("/v1/search", {
            "query": query_str, "user_id": self.user_id,
            "limit": self.k})
        block = out.get("context_block", "")
        if not block:
            return nodes
        n = min(self.prepend_to_top_n, len(nodes))
        for i in range(n):
            node = nodes[i]
            # duck-type: NodeWithScore has .node.text, simpler nodes
            # have .text directly
            inner = getattr(node, "node", node)
            cur = getattr(inner, "text", "") or ""
            setattr(inner, "text", f"[Memory]\n{block}\n\n[Retrieved]\n{cur}")
        return nodes

    # also expose the save_context method for symmetry with the
    # LangChain adapter — LlamaIndex query engines can call this from
    # a callback hook after each user turn
    def save_context(self, human_input: str, ai_output: str) -> None:
        msgs = []
        if human_input:
            msgs.append({"role": "user", "content": human_input})
        if ai_output:
            msgs.append({"role": "assistant", "content": ai_output})
        if not msgs:
            return
        self._post("/v1/add", {
            "messages": msgs, "user_id": self.user_id})


__all__ = ["ContextMMemoryPostprocessor"]
