"""HMS-style Cognition Engine — background self-organization.

The standout feature of holographic-memory (HMS): a background thread
that surfaces patterns, builds abstractions, detects knowledge gaps,
hypothesizes fillers, and finds analogies across domains.

This is the active part of memory — it turns a passive store into a
self-organizing knowledge base. When a user says "Alice works at Google"
and later "Alice moved to Mountain View," the engine should hypothesize
(Alice, lives_in, Mountain_View) and flag it for confirmation.

Architecture:
  PatternScanner   — surfaces structural regularities across triples
  AbstractionEngine — bundles atom vectors into prototypes when N
                       entities share a relation pattern
  GapDetector      — finds missing relations by comparing an entity's
                       profile to peers
  HypothesisEngine — proposes fillers for gaps via Hopfield cleanup
  AnalogyDetector  — finds structurally isomorphic domains via
                       bipartite relation mapping

Unlike HMS (which runs in a background thread), we trigger the engine
from `cortexm consolidate` — deterministic, auditable, no surprise
writes. Output is HYPOTHESIZED_BY edges in the Trace with confidence
< 0.5; never active in retrieval unless explicitly promoted.

The 5 stages run in a fixed pipeline so a single consolidate() call
produces a full self-organization sweep. Each stage reads the prior
stage's outputs via the Trace, so the engine is composable.
"""

from cortexm.cognition.scanner import PatternScanner
from cortexm.cognition.abstraction import AbstractionEngine
from cortexm.cognition.gaps import GapDetector, HypothesisEngine
from cortexm.cognition.analogy import AnalogyDetector
from cortexm.cognition.engine import (
    CognitionEngine,
    run_cognition_pass,
    HYPOTHESIZED_BY,
    PROMOTED_FROM,
)

__all__ = [
    "PatternScanner",
    "AbstractionEngine",
    "GapDetector",
    "HypothesisEngine",
    "AnalogyDetector",
    "CognitionEngine",
    "run_cognition_pass",
    "HYPOTHESIZED_BY",
    "PROMOTED_FROM",
]
