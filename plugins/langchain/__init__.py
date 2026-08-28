"""LangChain plugin for Context-M.

Drop-in memory adapter for LangChain / LangChain Community. Compatible
with both `BaseMemory` (legacy) and the new LangGraph state API.

Install:
    pip install context-m-langchain

Usage:
    from context_m_langchain import ContextMMemory
    memory = ContextMMemory(user_id="alice")
    agent = initialize_agent(tools, llm, memory=memory)

Or with the new LangGraph-native API:
    from context_m_langchain import context_m_checkpointer
    builder = StateGraph(AgentState, checkpointer=context_m_checkpointer())
"""
from context_m_memory import ContextMMemory  # noqa: F401

__version__ = "0.1.0"
__all__ = ["ContextMMemory"]
