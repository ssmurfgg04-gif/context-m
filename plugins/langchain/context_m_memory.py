"""LangChain BaseMemory adapter for Context-M.

LangChain's BaseMemory abstraction (langchain_community.memory) is the
canonical "drop-in memory" interface for any agent. Mem0 and Zep ship
adapters; Context-M needs one too — otherwise the LangChain community
defaults to those competitors.

Usage:
    from context_m.plugins.langchain import ContextMMemory
    memory = ContextMMemory(user_id="alice")
    # in an agent setup:
    #   agent = initialize_agent(tools, llm, memory=memory)
    memory.save_context({"input": "Hi I'm Alice"},
                        {"output": "Nice to meet you, Alice!"})
    # next turn:
    memory.load_memory_variables({"input": "what's my name?"})
    # → {"history": "Alice mentioned her name is Alice\\n..."}

This adapter calls Context-M's REST API (default http://localhost:8900)
so it works with any deployed Context-M instance — same wire format as
the MCP server, same provenance, same μ=0 ingest. No LLM calls in the
save path; load_memory_variables does a single /v1/search.

The adapter avoids a hard langchain dependency at import time — it
imports BaseMemory lazily so this module can be vendored into the main
package without forcing users to install langchain.
"""
from __future__ import annotations

import json
import os
from typing import Any
from urllib import request as urlreq
from urllib.error import URLError


class ContextMMemory:
    """LangChain BaseMemory adapter — calls Context-M REST API.

    The class duck-types BaseMemory (memory_key, return_messages,
    load_memory_variables, save_context, clear). We don't subclass
    BaseMemory directly to avoid a hard langchain dep at import time;
    langchain_community's BaseMemory subclass check is duck-typed.

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
                 timeout: float = 5.0) -> None:
        self.rest_url = (rest_url or os.environ.get("CONTEXT_M_REST_URL")
                          or "http://localhost:8900").rstrip("/")
        self.api_key = (api_key or os.environ.get("CONTEXT_M_API_KEY")
                        or "")
        self.user_id = user_id
        self.memory_key = memory_key
        self.return_messages = return_messages
        self.k = k
        self.timeout = timeout

    @property
    def memory_variables(self) -> list[str]:
        return [self.memory_key]

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
            # if the REST server is down, fail soft — return empty so
            # the agent still runs, just without memory recall
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

    # ------------------------------------------------------------------
    # BaseMemory interface
    # ------------------------------------------------------------------
    def load_memory_variables(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Fetch the relevant context block for the current input.

        Extracts the user's latest message from `inputs` (using the
        common keys: input, question, query, user_input), runs a
        /v1/search against Context-M, and returns the context block
        as a string under self.memory_key.
        """
        query = (inputs.get("input") or inputs.get("question")
                 or inputs.get("query") or inputs.get("user_input") or "")
        if not query:
            return {self.memory_key: "" if not self.return_messages else []}
        out = self._post("/v1/search", {
            "query": query, "user_id": self.user_id, "limit": self.k})
        block = out.get("context_block", "")
        if self.return_messages:
            # split into pseudo-messages for back-compat with agents that
            # expect a list of {role, content} dicts
            return {self.memory_key: [
                {"role": "system", "content": block}]}
        return {self.memory_key: block}

    def save_context(self, inputs: dict[str, Any],
                     outputs: dict[str, Any]) -> None:
        """Persist a (input, output) turn to Context-M.

        Sends both as a single message list — Context-M's extractor
        treats the human turn as the fact-bearing message and the AI
        turn as context. μ=0 — no LLM calls.
        """
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
        """No-op for now — Context-M's bi-temporal design means
        facts are never hard-deleted. Subclass and override to call
        /v1/governance/retention if you need a true clear."""
        pass

    def __repr__(self) -> str:
        return (f"ContextMMemory(rest_url={self.rest_url!r}, "
                f"user_id={self.user_id!r}, k={self.k})")


__all__ = ["ContextMMemory"]
