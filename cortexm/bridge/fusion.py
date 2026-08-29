"""Fusion Bridge — combine verbatim + structured retrieval, μ=0.

Reddit deep-dive (2026-08-29) + MemPalace comparison: the system
now has TWO tiers. The router picks which tier(s) to query; the
fusion bridge combines their results into a single ranked list.

Fusion algorithm (μ=0, no LLM):

  1. Collect hits from each tier (VerbatimHit, StructuredHit).
  2. Normalize each tier's scores to [0,1] (min-max within tier).
  3. Cross-tier weighted fusion:
       - If router picked ['verbatim'] only: weight 1.0 verbatim
       - If router picked ['structured'] only: weight 1.0 structured
       - If both: weight the FIRST tier 0.65, second 0.35
         (the router's "preferred" tier gets the boost)
  4. PRF (pseudo-relevance feedback) expand: take top-3 hits,
     extract their content words, re-query each tier with the
     expanded query. Boost hits that surface in both rounds.
  5. MIND diversity: penalize top-k if diversity < threshold.
  6. Return fused ranked list.

All operations are deterministic (same input → same output) and
μ=0 (no LLM at any step). The bridge preserves the five promises.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cortexm.router import route


@dataclass
class FusedHit:
    """One retrieval result after fusion."""
    tier: str               # "verbatim" or "structured"
    score: float            # fused score [0,1]
    payload: dict           # the original hit (verbatim.to_dict or structured.to_dict)
    boost_reason: str = ""  # why this hit got boosted (audit trail)

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "score": round(self.score, 4),
            "payload": self.payload,
            "boost_reason": self.boost_reason,
        }


class FusionBridge:
    """Combine verbatim + structured retrieval results.

    The bridge is STATELESS — it holds no references to the tiers.
    The caller passes the tier services + the query, the bridge
    returns a fused list. This makes it trivially testable and
    avoids any circular dependency between the bridge and the
    plugins.
    """

    # Cross-tier fusion weights when both tiers ran
    FIRST_TIER_WEIGHT = 0.65
    SECOND_TIER_WEIGHT = 0.35

    # PRF (pseudo-relevance feedback) — top-N hits to expand from
    PRF_TOPN = 3
    PRF_EXPAND_TERMS = 8
    PRF_BOOST = 0.15  # additive boost for hits that surface in round 2

    # MIND diversity penalty
    DIVERSITY_THRESHOLD = 0.85
    DIVERSITY_PENALTY = 0.20

    def __init__(self, *, first_tier_weight: float | None = None,
                 prf_enabled: bool = True,
                 diversity_penalty_enabled: bool = True) -> None:
        if first_tier_weight is not None:
            self.FIRST_TIER_WEIGHT = first_tier_weight
            self.SECOND_TIER_WEIGHT = 1.0 - first_tier_weight
        self.prf_enabled = prf_enabled
        self.diversity_penalty_enabled = diversity_penalty_enabled

    def fuse(self, *, query: str, user_id: str, k: int = 10,
             verbatim=None, structured=None,
             embedder=None) -> list[FusedHit]:
        """Run the router + both tiers + fusion.

        ``verbatim`` and ``structured`` are the plugin service
        objects (or None if not mounted). Either can be None —
        the bridge gracefully runs with whichever tiers are
        available.
        """
        # 1. Route
        tiers = route(query)

        # 2. Run each tier the router picked
        tier_results: dict[str, list] = {}
        for t in tiers:
            if t == "verbatim" and verbatim is not None:
                tier_results[t] = verbatim.search(
                    query=query, user_id=user_id, k=k)
            elif t == "structured" and structured is not None:
                tier_results[t] = structured.search(
                    query=query, user_id=user_id, k=k)

        # 3. Normalize within each tier
        normalized: dict[str, list[tuple[float, dict, str]]] = {}
        for tier_name, hits in tier_results.items():
            if not hits:
                continue
            scores = [getattr(h, "score", 0.5) for h in hits]
            mn, mx = min(scores), max(scores)
            rng = (mx - mn) if (mx - mn) > 1e-9 else 1.0
            for h, s in zip(hits, scores):
                norm_s = (s - mn) / rng
                payload = h.to_dict() if hasattr(h, "to_dict") else dict(h)
                # Annotate the payload's tier so downstream can filter
                payload["tier"] = tier_name
                # Stash a one-line boost reason for the audit trail
                boost_reason = ""
                if norm_s >= 0.9:
                    boost_reason = f"top-of-tier ({tier_name})"
                normalized.setdefault(tier_name, []).append(
                    (norm_s, payload, boost_reason))

        # 4. Cross-tier weighted fusion
        fused: list[FusedHit] = []
        for i, tier_name in enumerate(tiers):
            if tier_name not in normalized:
                continue
            weight = (self.FIRST_TIER_WEIGHT if i == 0
                       else self.SECOND_TIER_WEIGHT)
            # If only one tier ran, weight = 1.0 regardless
            if len(tiers) == 1:
                weight = 1.0
            for norm_s, payload, boost_reason in normalized[tier_name]:
                fused.append(FusedHit(
                    tier=tier_name,
                    score=norm_s * weight,
                    payload=payload,
                    boost_reason=boost_reason))

        # 5. PRF expand (optional, μ=0)
        if self.prf_enabled and len(fused) >= 3 and (
                verbatim is not None or structured is not None):
            self._prf_expand(query=query, user_id=user_id,
                             fused=fused, tiers=tiers,
                             verbatim=verbatim, structured=structured)

        # 6. MIND diversity penalty (optional, μ=0)
        if self.diversity_penalty_enabled and embedder is not None:
            self._apply_diversity_penalty(fused, embedder)

        # 7. Sort + truncate
        fused.sort(key=lambda h: h.score, reverse=True)
        return fused[:k]

    # ---------------------------- PRF --------------------------------

    def _prf_expand(self, *, query: str, user_id: str,
                    fused: list[FusedHit], tiers: list[str],
                    verbatim=None, structured=None) -> None:
        """Pseudo-relevance feedback: expand query from top hits.

        Take the top-3 fused hits, extract their content words,
        build an expanded query, re-run each tier, and boost
        hits that appear in BOTH rounds.
        """
        from cortexm.text.tokenizer import content_words
        top_n = fused[:self.PRF_TOPN]
        expansion_terms: set[str] = set()
        for hit in top_n:
            # Pull text from the payload — verbatim has 'text',
            # structured has 'value' (the fact string)
            text = hit.payload.get("text") or hit.payload.get("value") or ""
            expansion_terms.update(content_words(text)[:self.PRF_EXPAND_TERMS])
        if not expansion_terms:
            return
        expanded_query = query + " " + " ".join(sorted(expansion_terms))

        # Re-run each tier with the expanded query
        round2_text_hits: set[str] = set()
        for t in tiers:
            if t == "verbatim" and verbatim is not None:
                hits = verbatim.search(query=expanded_query, user_id=user_id,
                                       k=self.PRF_TOPN * 2)
                for h in hits:
                    round2_text_hits.add((h.text or "")[:80])
            elif t == "structured" and structured is not None:
                hits = structured.search(query=expanded_query, user_id=user_id,
                                          k=self.PRF_TOPN * 2)
                for h in hits:
                    round2_text_hits.add((h.value or "")[:80])

        # Boost fused hits whose text/value also appears in round 2
        for hit in fused:
            text = hit.payload.get("text") or hit.payload.get("value") or ""
            if (text or "")[:80] in round2_text_hits:
                hit.score += self.PRF_BOOST
                hit.boost_reason = (hit.boost_reason +
                                    " | prf-boosted" if hit.boost_reason
                                    else "prf-boosted").strip(" |")

    # ---------------------------- MIND diversity --------------------

    def _apply_diversity_penalty(self, fused: list[FusedHit],
                                  embedder) -> None:
        """If top-k is too similar, penalize the cluster.

        This is the MIND defense (cortexm.security.mind) applied at
        the fusion layer. We compute pairwise cosine among top-k
        hits; if mean sim > threshold, scale down scores of the
        clustered hits.
        """
        if len(fused) < 2 or embedder is None:
            return
        try:
            import numpy as np
            texts = [h.payload.get("text") or h.payload.get("value") or ""
                      for h in fused[:10]]
            embs = np.stack([embedder.embed(t) for t in texts])
            norms = np.linalg.norm(embs, axis=1, keepdims=True)
            norms = np.where(norms == 0, 1.0, norms)
            embs_n = embs / norms
            sim_mat = embs_n @ embs_n.T
            n = sim_mat.shape[0]
            if n < 2:
                return
            iu = np.triu_indices(n, k=1)
            mean_sim = float(np.mean(sim_mat[iu]))
            if mean_sim > self.DIVERSITY_THRESHOLD:
                # Penalize — scale down all hits proportionally to
                # their mean similarity to other hits
                row_means = sim_mat.mean(axis=1)
                for i, hit in enumerate(fused[:10]):
                    if row_means[i] > self.DIVERSITY_THRESHOLD:
                        hit.score *= (1.0 - self.DIVERSITY_PENALTY)
                        hit.boost_reason = (
                            hit.boost_reason +
                            " | mind-penalized" if hit.boost_reason
                            else "mind-penalized").strip(" |")
        except Exception:
            pass  # μ=0 — never let diversity math kill retrieval


__all__ = ["FusionBridge", "FusedHit"]
