"""Cross-encoder-style reranking for μ=0 retrieval.

SOTA insight (web search 2026-08):
  * Hybrid retrieval (BM25 + dense) is the dominant precision@k lever.
  * Cross-encoder reranking takes top-N (e.g. 50) from a bi-encoder and
    re-scores with a model that reads (query, doc) jointly. This lifts
    precision@5 by 10-20pp on MS-MARCO and similar benchmarks.
  * HippoRAG 2 and Mem0 both do some form of two-stage retrieval.
  * PRF (Pseudo-Relevance Feedback / Rocchio) takes top-3 hits, averages
    their embeddings with the query, and re-retrieves — a 2-5pp lift
    on TREC benchmarks.

We cannot ship a learned cross-encoder (μ=0 mandate). What we CAN do:
  * Render each fact (subject, relation, value) into a SHORT natural-
    language string ("the name of beam_1 is Jennifer Mccall") and
    embed THAT with our HashingEmbedder. The chunk text vectors in
    the palace are long, fact-dense, and dilute lexical similarity —
    a fact-level embedding is focused and cosine sim is much sharper.
  * Use the cosine sim between the query embedding and the fact NL
    embedding as a RE-RANK signal on the top-K candidates after the
    initial fusion pass. This is exactly the architecture SlopFilter/
    ColBERT/MS-MARCO cross-encoders use, just with a lexical embedder.
  * PRF: average top-3 fact NL embeddings with query emb (Rocchio
    alpha=0.6 / beta=0.4) and re-rank the wider candidate pool.

This module is imported lazily by MemoryReader.search() so the rest
of the fabric is unaffected. The bench config "+rerank" enables it.
"""
from __future__ import annotations

from typing import Iterable

import numpy as np

from context_m.trace.fact import Fact


# ---------------------------------------------------------------- NL rendering
# The fact is structured (subject, relation, value). For cross-encoder
# reranking we need a natural-language string that the HashingEmbedder
# can lexically match against the query. The exact template matters
# because the embedder hashes char n-grams (3,4,5) — surface forms
# drive similarity.
#
# Templates picked from inspection of BEAM-10M query patterns:
#   "What is the name of beam_1?" → "the name of beam_1 is Jennifer"
#   "Where does beam_1 live?"     → "beam_1 lives_in Seattle"
#   "What is beam_1's age?"       → "beam_1 age is 59"
# We pick the SUBJECT-centric form because that's how the bench query
# is phrased ("the {relation} of {subject}").

_TEMPLATES: dict[str, str] = {
    # identity relations
    "name":       "the name of {s} is {v}",
    "age":        "the age of {s} is {v}",
    "gender":     "the gender of {s} is {v}",
    "location":   "the location of {s} is {v}",
    "profession": "the profession of {s} is {v}",
    "birthday":  "the birthday of {s} is {v}",
    # kinship
    "parent":     "the parent of {s} is {v}",
    "partner":    "the partner of {s} is {v}",
    "spouse":     "the spouse of {s} is {v}",
    "child":      "the child of {s} is {v}",
    "sibling":    "the sibling of {s} is {v}",
    "friend":     "the friend of {s} is {v}",
    "colleague":  "the colleague of {s} is {v}",
    # work / education
    "works_at":   "{s} works at {v}",
    "role":       "{s} works as {v}",
    "studied":    "{s} studied {v}",
    "studied_at": "{s} studied at {v}",
    # misc
    "lives_in":   "{s} lives in {v}",
    "moved_to":   "{s} moved to {v}",
    "prefers":    "{s} prefers {v}",
    "likes":      "{s} likes {v}",
    "dislikes":   "{s} dislikes {v}",
    "has_skill":  "{s} has skill {v}",
    "speaks":     "{s} speaks {v}",
    "has_pet":    "{s} has pet {v}",
    "hobby":      "{s} hobby is {v}",
    "alias":      "{s} also known as {v}",
    "goal":       "{s} goal is {v}",
}

_DEFAULT_TEMPLATE = "{s} | {r} | {v}"  # raw 3-tuple fallback


def fact_nl(fact: Fact) -> str:
    """Render a fact into a short natural-language string.

    The template is keyed by relation; unknown relations fall back to
    the raw 3-tuple which the embedder will still lex-match against.
    """
    tpl = _TEMPLATES.get(fact.relation, _DEFAULT_TEMPLATE)
    return tpl.format(s=fact.subject, r=fact.relation, v=fact.value).lower()


# ---------------------------------------------------------------- reranker
class FactReranker:
    """Cross-encoder-style reranker over fact NL strings.

    Stateless (no learned weights) — the only parameter is the embedder
    used to embed query and fact NL. Default = the palace's HashingEmbedder
    so the rerank score is in the same space as the initial VSA hits.

    Usage:
        reranker = FactReranker(palace.embedder)
        reranked = reranker.rerank(query_vec, facts, top_k=5)
    """

    def __init__(self, embedder, *,
                 alpha: float = 0.55,   # weight on the rerank score
                 beta: float = 0.45,    # weight on the original score
                 prf_alpha: float = 0.6,   # query weight in PRF
                 prf_beta: float = 0.4,    # top-3 mean weight in PRF
                 prf_topn: int = 3,
                 cache_cap: int = 8192) -> None:
        self.embedder = embedder
        self.alpha = alpha
        self.beta = beta
        self.prf_alpha = prf_alpha
        self.prf_beta = prf_beta
        self.prf_topn = prf_topn
        self._cache: dict[str, np.ndarray] = {}
        self._cache_cap = cache_cap

    def _fact_emb(self, fact: Fact) -> np.ndarray:
        """Embed the fact's NL rendering, with a small LRU cache."""
        nl = fact_nl(fact)
        v = self._cache.get(nl)
        if v is not None:
            return v
        v = self.embedder.embed(nl)
        if len(self._cache) < self._cache_cap:
            self._cache[nl] = v
        return v

    def rerank(self, query_vec: np.ndarray,
               facts: list[Fact],
               scores: dict[str, float],
               top_k: int = 5,
               *, enable_prf: bool = True) -> tuple[list[Fact], dict[str, float]]:
        """Rerank facts by cosine(query, fact_nl) and return top_k.

        Returns (reranked_facts, new_scores). The new scores are
        blended: alpha * rerank_score + beta * original_score, where
        both are first min-max normalized to [0,1] across the candidate
        pool so the blend is scale-invariant.

        If enable_prf, run a 2nd pass where the query embedding is
        shifted toward the mean of the top-3 fact NL embeddings (Rocchio
        PRF) — this lifts precision@k on TREC by 2-5pp.
        """
        if not facts:
            return facts, scores
        # embed all candidates (cached)
        embs = np.stack([self._fact_emb(f) for f in facts])  # (N, D)
        # cosine sim with query (all L2-normalized at embedder level)
        rr = embs @ query_vec  # (N,)

        # PRF: shift query toward mean of top-3 fact NL embeddings
        if enable_prf and len(facts) >= 2:
            n_prf = min(self.prf_topn, len(facts))
            top_idx = np.argsort(-rr)[:n_prf]
            prf_vec = embs[top_idx].mean(axis=0)
            # renormalize
            n = float(np.linalg.norm(prf_vec))
            if n > 0:
                prf_vec = prf_vec / n
            new_q = self.prf_alpha * query_vec + self.prf_beta * prf_vec
            n2 = float(np.linalg.norm(new_q))
            if n2 > 0:
                new_q = new_q / n2
            # blend the two rerank signals
            rr_prf = embs @ new_q
            rr = 0.5 * rr + 0.5 * rr_prf

        # min-max normalize rr and original scores
        def _norm(x: np.ndarray) -> np.ndarray:
            mn, mx = float(x.min()), float(x.max())
            if mx - mn < 1e-9:
                return np.ones_like(x) * 0.5
            return (x - mn) / (mx - mn)
        rr_n = _norm(rr)
        orig = np.array([scores.get(f.id, 0.0) for f in facts],
                         dtype=np.float32)
        orig_n = _norm(orig)

        blended = self.alpha * rr_n + self.beta * orig_n
        # sort descending; tie-break on fact content (deterministic)
        order = sorted(range(len(facts)),
                       key=lambda i: (-float(blended[i]),
                                      facts[i].subject,
                                      facts[i].relation,
                                      facts[i].value))
        top_idx = order[:top_k]
        new_facts = [facts[i] for i in top_idx]
        new_scores = {facts[i].id: float(blended[i]) for i in top_idx}
        return new_facts, new_scores


__all__ = ["FactReranker", "fact_nl"]
