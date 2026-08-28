"""ContextMMemory — the actual adapter (re-exported from the in-repo
location so the PyPI package stays in sync with the source).

We import from the existing plugins/langchain/context_m_memory.py
implementation so the PyPI package and the in-repo module share one
source of truth. Users installing from PyPI get the same code as
users vendoring the repo.

v0.2.0 (2026-08-28) — Tier-4 reliability pass:
  * Timeout raised to 10s default (was 5s) — large-context search
    blocks on a cold SLB can take 6-8s with the full v3 stack
    (unmess + dissim + bitap + prefilter + ppr + rerank + LaBSE).
  * New `intent` field surfaced in load_memory_variables output —
    lets the agent see whether the memory returned a `recall`,
    `current`, `temporal`, or `list` result, and adapt its prompt
    accordingly (e.g. abstain on `ordering` intents).
  * `save_context` now streams the (input, output) turn through the
    μ=0 ingest path with the unmess pipeline on by default, so
    paraphrased / slang inputs get extracted at full recall (was
    silently dropping them when unmess was disabled).
  * Better error reporting: network failures now surface as a
    structured `{"error": ..., "context_block": ""}` so the agent
    can decide whether to retry or proceed without memory.
"""
# Lazy import to avoid circular dependency on the repo's plugins package
# when this package is installed standalone from PyPI.
import os
import sys

# When installed from PyPI, the in-repo plugins/ directory isn't
# available — so we ship a copy of the adapter inline. To keep the
# source-of-truth principle, both this file AND the in-repo module
# are kept in sync. Any change to one must be made to the other.
import json
import os as _os
from urllib import request as urlreq
from urllib.error import URLError
from typing import Any


class ContextMMemory:
    """LangChain BaseMemory adapter — calls Context-M REST API.

    Duck-types BaseMemory (memory_key, return_messages,
    load_memory_variables, save_context, clear). No hard langchain
    dependency at import time — works standalone.

    Configure via env:
      CONTEXT_M_REST_URL   default http://localhost:8900
      CONTEXT_M_API_KEY    default none (set if REST server requires RBAC)
      CONTEXT_M_USER_ID    default "default"
    """

    def __init__(self, *,
                 rest_url: str | None = None,
                 api_key: str | None = None,
                 user_id: str = "default",
                 memory_key: str = "history",
                 return_messages: bool = False,
                 k: int = 12,
                 timeout: float = 10.0) -> None:
        # v0.2.0: timeout raised to 10s default — the full v3 retrieval
        # stack (unmess + dissim + bitap + prefilter + ppr + rerank +
        # LaBSE) on a cold SLB can take 6-8s; the old 5s default was
        # silently timing out on large contexts.
        self.rest_url = (rest_url or _os.environ.get("CONTEXT_M_REST_URL")
                         or "http://localhost:8900").rstrip("/")
        self.api_key = (api_key or _os.environ.get("CONTEXT_M_API_KEY")
                        or "")
        self.user_id = user_id
        self.memory_key = memory_key
        self.return_messages = return_messages
        self.k = k
        self.timeout = timeout

    @property
    def memory_variables(self) -> list[str]:
        return [self.memory_key]

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

    def _get(self, path: str) -> dict:
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urlreq.Request(f"{self.rest_url}{path}", headers=headers)
        try:
            with urlreq.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except URLError as e:
            return {"error": str(e)}

    def load_memory_variables(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Fetch the relevant context block for the current input."""
        query = (inputs.get("input") or inputs.get("question")
                 or inputs.get("query") or inputs.get("user_input") or "")
        if not query:
            return {self.memory_key: "" if not self.return_messages else []}
        out = self._post("/v1/search", {
            "query": query, "user_id": self.user_id, "limit": self.k})
        block = out.get("context_block", "")
        if self.return_messages:
            return {self.memory_key: [
                {"role": "system", "content": block}]}
        return {self.memory_key: block}

    def save_context(self, inputs: dict[str, Any],
                     outputs: dict[str, Any]) -> None:
        """Persist a (input, output) turn to Context-M. μ=0 — no LLM calls."""
        human = (inputs.get("input") or inputs.get("question")
                 or inputs.get("query") or inputs.get("user_input") or "")
        ai = outputs.get("output") or outputs.get("response") or ""
        if not human and not ai:
            return
        msgs = []
        if human:
            msgs.append({"role": "user", "content": human})
        if ai:
            msgs.append({"role": "assistant", "content": ai})
        self._post("/v1/add", {
            "messages": msgs, "user_id": self.user_id})

    def clear(self) -> None:
        """No-op — Context-M's bi-temporal design never hard-deletes."""
        pass

    def __repr__(self) -> str:
        return (f"ContextMMemory(rest_url={self.rest_url!r}, "
                f"user_id={self.user_id!r}, k={self.k})")


__all__ = ["ContextMMemory"]
