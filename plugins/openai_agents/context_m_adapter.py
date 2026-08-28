"""OpenAI Agents SDK memory adapter for Context-M.

The OpenAI Agents SDK (H2 2026) replaces the older Assistants API and
is the fastest-growing agent framework. Its memory model is a
"memory tool" the agent calls — same shape as our MCP server, but
the SDK wants a Python-callable function, not a JSON-RPC service.

This adapter exposes Context-M as the two functions the SDK expects:
  - recall(query, user_id) -> str    : fetch the relevant context block
  - remember(messages, user_id) -> int : persist a turn, returns n stored

Both call the Context-M REST API so this works with any deployment.

Usage:
    from context_m_openai_agents import recall, remember, make_tools
    from agents import Agent, Runner

    agent = Agent(
        name="Alice-assistant",
        instructions="You are a helpful assistant.",
        tools=make_tools(user_id="alice"),
    )
    result = Runner.run_sync(agent, "what's my sister's name?")
"""
from __future__ import annotations

import json
import os
from typing import Any
from urllib import request as urlreq
from urllib.error import URLError


_DEFAULT_URL = (os.environ.get("CONTEXT_M_REST_URL")
                or "http://localhost:8900").rstrip("/")
_DEFAULT_KEY = os.environ.get("CONTEXT_M_API_KEY") or ""


def _post(path: str, body: dict, *, rest_url: str | None = None,
          api_key: str | None = None, timeout: float = 5.0) -> dict:
    url = (rest_url or _DEFAULT_URL) + path
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    key = api_key or _DEFAULT_KEY
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urlreq.Request(url, data=data, headers=headers, method="POST")
    try:
        with urlreq.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except URLError as e:
        return {"error": str(e), "context_block": ""}


def recall(query: str, *, user_id: str = "default",
           k: int = 12, rest_url: str | None = None,
           api_key: str | None = None) -> str:
    """Recall the relevant context block for the user's query.

    Calls /v1/search on the Context-M REST server. Returns the
    context_block string (the LLM-ready formatted memory view).
    On error returns "" — the agent still runs, just without recall.
    """
    out = _post("/v1/search", {"query": query, "user_id": user_id,
                                "limit": k}, rest_url=rest_url,
                api_key=api_key)
    return out.get("context_block", "")


def remember(messages: list[dict[str, str]] | str,
             *, user_id: str = "default",
             rest_url: str | None = None,
             api_key: str | None = None) -> int:
    """Persist a turn (or list of turns) to Context-M.

    `messages` can be a string (treated as a single user turn) or a
    list of {"role", "content"} dicts. Returns the number of stored
    facts (from the API response stats). μ=0 — no LLM calls.
    """
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    out = _post("/v1/add", {"messages": messages, "user_id": user_id},
                rest_url=rest_url, api_key=api_key)
    return len(out.get("results", [])) if isinstance(out, dict) else 0


# ---------------------------------------------------------------------------
# Agent-tool wrappers — call these from the OpenAI Agents SDK.
# ---------------------------------------------------------------------------

def make_tools(*, user_id: str = "default", k: int = 12,
               rest_url: str | None = None, api_key: str | None = None):
    """Build the recall + remember tool callables for the OpenAI Agents SDK.

    Returns a list of @function_tool-decorated callables. Importing
    `agents` is deferred so this module is usable without it.
    """
    try:
        from agents import function_tool
    except ImportError:
        def recall_tool(query: str) -> str:
            """Fetch the user's relevant memory context for this query."""
            return recall(query, user_id=user_id, k=k,
                          rest_url=rest_url, api_key=api_key)

        def remember_tool(content: str) -> int:
            """Persist a user message to memory. Returns count stored."""
            return remember(content, user_id=user_id, rest_url=rest_url,
                            api_key=api_key)
        return [recall_tool, remember_tool]

    @function_tool
    def contextm_recall(query: str) -> str:
        """Fetch the user's relevant memory context for this query.

        Use this tool BEFORE answering to retrieve what the agent
        already knows about the user, their preferences, past
        conversations, and any instructions. Returns a formatted
        context block (LLM-ready).
        """
        return recall(query, user_id=user_id, k=k, rest_url=rest_url,
                      api_key=api_key)

    @function_tool
    def contextm_remember(content: str) -> int:
        """Persist a user message to memory.

        Call this AFTER the user's turn so future turns can recall
        what was said. Returns the number of facts extracted (0 is
        fine — the message just didn't contain extractable facts).
        """
        return remember(content, user_id=user_id, rest_url=rest_url,
                        api_key=api_key)

    return [contextm_recall, contextm_remember]


__all__ = ["recall", "remember", "make_tools"]
