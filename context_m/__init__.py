"""Context-M — The Universal Neuro-Symbolic Memory Fabric.

Layer 1  Symbolic Trace   : bi-temporal fact graph with contradiction
                            resolution, temporal edges, Datalog-lite rules.
Layer 2  VSA Memory Palace: holographic reduced representations (HRR) with
                            INT8 / Binary-HRR / RaBitQ / PQ codecs, a
                            page-clustered tree index and a semantic
                            lookaside buffer (SLB).
Bridge  : μ=0 deterministic ingest (zero LLM calls), neuro-symbolic read
          path with cryptographic provenance on every retrieval.

Mem0-compatible surface:  ``from context_m import Memory``
Alias (plan naming):      ``from cortexm import Memory``
"""

from __future__ import annotations

__version__ = "0.1.0"

# μ=0 protocol counter: number of LLM invocations used by this process.
# The BEAM-honest protocol requires this to stay 0 during ingest & retrieval.
LLM_CALLS = 0


def _lazy_memory():
    from context_m.api.memory import Memory

    return Memory


def __getattr__(name: str):
    if name == "Memory":
        return _lazy_memory()
    if name == "Config":
        from context_m.config import Config

        return Config
    if name == "LLM_CALLS":
        from context_m import metrics

        return metrics.llm_calls()
    raise AttributeError(name)


__all__ = ["Memory", "Config", "LLM_CALLS", "__version__"]
