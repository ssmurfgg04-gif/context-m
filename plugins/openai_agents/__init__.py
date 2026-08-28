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
from context_m_adapter import recall, remember, make_tools  # noqa: F401

__version__ = "0.1.0"
__all__ = ["recall", "remember", "make_tools"]
