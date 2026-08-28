"""MIND-style retrieval diversity defense against InjecMEM.

arXiv:2608.23471 (InjecMEM, August 2026) attack taxonomy:
  * Retriever-agnostic anchors — high-recall topical cues that
    guarantee the poisoned record surfaces in top-k
  * Adversarial commands optimized via gradient-based coordinate
    search (Multi-GCG)
  * Achieves 76.6% conditional ASR when poisoned records are retrieved

arXiv:2607.28103 (MIND) defense:
  * Intent-aware Information Bottleneck extracts compact intent-
    behavior representations from multi-turn trajectories
  * Detects poisoned memories by measuring deviation between initial
    user intent and subsequent behavior — exactly the signal InjecMEM
    exploits
  * MIND achieves the lowest ASR (19.57%) while preserving task
    accuracy and running 20.6% faster than LLM auditing

We can't ship the full MIND (it requires a learned intent encoder).
But we CAN ship the SECONDARY signal MIND relies on: retrieval
diversity scoring.

InjecMEM relies on centroid anchors that CLUSTER in embedding space —
the attacker wants multiple poisoned records to surface so the LLM
sees the malicious instruction reinforced. If the top-k results are
all too similar (low intra-result diversity), that's a strong signal
of anchor-based poisoning.

This module exposes:
  * retrieval_diversity(facts, embedder) — mean pairwise cosine sim
    among the top-k results. High mean = low diversity = suspect.
  * mind_check(facts, embedder, threshold) — returns a MINDVerdict
    with .flagged=True if diversity is too low.
  * augment_provenance(result, mind_verdict) — stamps the retrieval
    result's provenance with the diversity score so downstream audit
    dashboards can surface flagged retrievals.

This is μ=0 compatible — pure embedding math, no learned weights, no
LLM call. The existing InjecMEM (regex + contagion) and MINJA
(taint) defenses remain unchanged; MIND diversity is a third layer
that catches what they miss: gradient-optimized adversarial text
that is semantically coherent until retrieved.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np

from context_m.bridge.rerank import fact_nl
from context_m.trace.fact import Fact


@dataclass
class MINDVerdict:
    """MIND diversity check verdict.

    diversity: float in [0,1] — mean pairwise cosine sim among top-k.
                 1.0 = all facts identical (maximally suspect)
                 0.0 = all facts orthogonal (maximally diverse)
    flagged: True if diversity > threshold (low diversity = suspect)
    reason: human-readable explanation
    """
    diversity: float = 0.0
    flagged: bool = False
    threshold: float = 0.85
    n_facts: int = 0
    reason: str = ""
    fact_ids: list[str] = field(default_factory=list)


def _cosine_matrix(embs: np.ndarray) -> np.ndarray:
    """All-pairs cosine sim. Assumes embs are L2-normalized (HashingEmbedder
    guarantees this). Falls back to manual normalization for safety.
    """
    norms = np.linalg.norm(embs, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    embs_n = embs / norms
    return embs_n @ embs_n.T


def retrieval_diversity(facts: list[Fact], embedder,
                         use_fact_nl: bool = True) -> float:
    """Mean pairwise cosine similarity among the top-k retrieved facts.

    Lower = more diverse = healthier.
    Higher = more clustered = suspect (possible InjecMEM anchor).
    """
    if len(facts) < 2:
        return 0.0
    embs = []
    for f in facts:
        try:
            text = fact_nl(f) if use_fact_nl else f.value
            embs.append(embedder.embed(text))
        except Exception:
            v = np.zeros(getattr(embedder, "dims", 768), dtype=np.float32)
            for i, ch in enumerate((f.value or "")[:768]):
                v[i] = (ord(ch) % 7 - 3) / 3.0
            embs.append(v)
    mat = _cosine_matrix(np.stack(embs))
    n = mat.shape[0]
    iu = np.triu_indices(n, k=1)
    return float(np.mean(mat[iu]))


def mind_check(facts: list[Fact], embedder,
               threshold: float = 0.85,
               min_facts: int = 2,
               flag_on_low_diversity: bool = True) -> MINDVerdict:
    """Run the MIND diversity check on a retrieval result."""
    if len(facts) < min_facts:
        return MINDVerdict(
            diversity=0.0, flagged=False, threshold=threshold,
            n_facts=len(facts),
            reason=f"too few facts ({len(facts)} < {min_facts}) to "
                   f"compute diversity",
            fact_ids=[f.id for f in facts])
    div = retrieval_diversity(facts, embedder)
    flagged = flag_on_low_diversity and div > threshold
    reason = (
        f"diversity={div:.3f} (threshold={threshold:.3f}) — "
        + ("FLAGGED: low diversity suggests possible InjecMEM "
           "anchor-based poisoning; recommend audit."
           if flagged else
           "OK: retrieval set is sufficiently diverse.")
    )
    return MINDVerdict(
        diversity=div, flagged=flagged, threshold=threshold,
        n_facts=len(facts), reason=reason,
        fact_ids=[f.id for f in facts])


def augment_provenance(result, verdict: MINDVerdict) -> None:
    """Stamp a RetrievalResult's provenance with the MIND diversity score."""
    try:
        if hasattr(result, "provenance") and isinstance(
                result.provenance, dict):
            result.provenance["mind_diversity"] = round(verdict.diversity, 4)
            result.provenance["mind_flagged"] = verdict.flagged
            result.provenance["mind_threshold"] = verdict.threshold
            if verdict.flagged:
                result.provenance["mind_reason"] = verdict.reason
    except Exception:
        pass


__all__ = [
    "MINDVerdict",
    "retrieval_diversity",
    "mind_check",
    "augment_provenance",
]
