"""cortexm.experimental — deterministic research borrows, μ=0.

Modules here are ideas ported from other memory systems (RuVector's
graph reconstruction and temporal-coherence gating, hippocampal
memory-index theories, etc.) re-implemented WITHOUT any learned or
probabilistic component: pure index lookups, counting, and arithmetic.

Every module in this package upholds the five core promises —
most importantly μ=0 (zero LLM calls, byte-deterministic outputs) —
or it does not ship.

Current residents (v0.6.4):
  * graph_recall  — entity→fact adjacency index + 2-hop walks
                    (targets the multi-session retrieval misses)
  * coherence     — temporal-coherence reranking signal
                    (targets the temporal_reasoning failures on
                    multi-week relative references)
"""
from cortexm.experimental.graph_recall import (
    EntityGraphIndex,
    graph_recall_boost,
)
from cortexm.experimental.coherence import coherence_scores

__all__ = [
    "EntityGraphIndex",
    "graph_recall_boost",
    "coherence_scores",
]
