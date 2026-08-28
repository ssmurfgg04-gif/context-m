"""LlamaIndex plugin for Context-M.

NodePostprocessor that calls Context-M's /v1/search and prepends the
context block to retrieved nodes.

Install:
    pip install context-m-llamaindex

Usage:
    from context_m_llamaindex import ContextMMemoryPostprocessor
    postproc = ContextMMemoryPostprocessor(user_id="alice")
    query_engine = index.as_query_engine(
        node_postprocessors=[postproc])
"""
from context_m_postprocessor import ContextMMemoryPostprocessor  # noqa: F401

__version__ = "0.1.0"
__all__ = ["ContextMMemoryPostprocessor"]
