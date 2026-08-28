"""context-m-langchain — top-level package for the PyPI distribution.

Re-exports ContextMMemory so users install via `pip install
context-m-langchain` and import via `from context_m_langchain import
ContextMMemory`.
"""
from context_m_langchain.adapter import ContextMMemory

__all__ = ["ContextMMemory"]
__version__ = "0.2.0"
