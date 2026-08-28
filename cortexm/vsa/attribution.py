"""ProtoDash attribution — submodular prototype selection for retrieval.

Given a query embedding and a candidate set of retrieved chunks,
ProtoDash (arXiv:1707.01212) selects up to m prototypes that best
reconstruct the query in kernel space, with non-negative weights.

Produces an audit trail: for every retrieved fact, a weight in [0,1]
indicating its contribution to the query reconstruction. Stored in the
fact's provenance dict under {"protodash_weight": 0.32}.

Pure Python + numpy + scipy.optimize.nnls. Greedy submodular selection
gives a (1-1/e) approximation guarantee of the optimal selection.

Also provides sentence-level cosine similarity scoring — classifies
each retrieved sentence's contribution as Very High (>0.8), High (>0.6),
Medium (>0.4), Low (>0.2), or Negligible.

arxiv research: arXiv:1707.01212 (ProtoDash, 2017).
"""

from __future__ import annotations

import numpy as np


class ProtoDashAttributer:
    """Source attribution via submodular prototype selection."""

    def __init__(self, kernel: str = "linear", gamma: float = 0.1) -> None:
        self.kernel = kernel
        self.gamma = gamma

    def _k(self, A: np.ndarray, B: np.ndarray) -> np.ndarray:
        """Kernel matrix between two sets of embeddings."""
        if self.kernel == "linear":
            return A @ B.T
        # RBF kernel: exp(-gamma ||a-b||^2)
        sq = (np.sum(A ** 2, axis=1)[:, None]
              + np.sum(B ** 2, axis=1)[None, :]
              - 2 * A @ B.T)
        return np.exp(-self.gamma * np.maximum(sq, 0))

    def attribute(self, query_emb: np.ndarray, candidate_embs: np.ndarray,
                 candidate_ids: list[str], m: int = 5
                ) -> list[tuple[str, float]]:
        """Return up to m (fact_id, weight) pairs that best reconstruct
        the query in kernel space. Weights are non-negative and
        normalized to sum to ~1.
        """
        if not candidate_ids:
            return []
        X = np.atleast_2d(query_emb.astype(np.float32))
        Y = np.atleast_2d(candidate_embs.astype(np.float32))
        n = len(candidate_ids)
        m = min(m, n)
        # kernel values
        Kxy = self._k(Y, X)[:, 0]    # (n,)
        Kyy_sum = float(self._k(X, X).sum())   # constant
        S_idx: list[int] = []
        for _ in range(m):
            best, best_gain = -1, -np.inf
            for j in range(n):
                if j in S_idx:
                    continue
                sj = Y[j:j + 1]
                # marginal gain: 2*k(y_j, x) - 2*sum_{s in S} k(y_j, s) - k(y_j, y_j)
                if S_idx:
                    Kss = self._k(sj, Y[S_idx]).sum()
                else:
                    Kss = 0.0
                Kjj = float(self._k(sj, sj)[0, 0])
                gain = 2.0 * Kxy[j] - 2.0 * Kss - Kjj
                if gain > best_gain:
                    best_gain, best = gain, j
            if best < 0 or best_gain <= 0:
                break
            S_idx.append(best)
        if not S_idx:
            return []
        # NNLS weights: solve K_SS w = K_SX, w >= 0
        try:
            from scipy.optimize import nnls
            S = Y[S_idx]
            K_SS = self._k(S, S) + 1e-6 * np.eye(len(S_idx))
            K_SX = self._k(S, X)[:, 0]
            w, _ = nnls(K_SS, K_SX)
            w = w / max(w.sum(), 1e-9)
        except Exception:
            # fallback to uniform
            w = np.ones(len(S_idx)) / len(S_idx)
        return [(candidate_ids[S_idx[i]], float(w[i])) for i in range(len(S_idx))]


def sentence_level_score(query: str, sentences: list[str],
                         embedder) -> list[dict]:
    """Score each sentence's contribution to the query.

    Returns list of {sentence, score, classification} dicts.
    Classification buckets: Very High (>0.8), High (>0.6), Medium (>0.4),
    Low (>0.2), Negligible.
    """
    if not sentences:
        return []
    q = embedder.embed(query)
    out = []
    for sent in sentences:
        if not sent.strip():
            continue
        e = embedder.embed(sent)
        cos = float(np.dot(q, e))
        if cos > 0.8:
            cls = "Very High"
        elif cos > 0.6:
            cls = "High"
        elif cos > 0.4:
            cls = "Medium"
        elif cos > 0.2:
            cls = "Low"
        else:
            cls = "Negligible"
        out.append({"sentence": sent, "score": cos, "classification": cls})
    return out


# Retrieval path tag enum — assigned to every retrieved fact in the audit trail
RETRIEVAL_PATHS = (
    "vsa_unbind",         # holographic overlay direct unbind
    "pattern_match",      # deterministic pattern extractor
    "neural_fallback",    # LLM enrichment path (opt-in)
    "raw_chunk",          # raw text chunk fallback
    "tree_index",         # TreeIndex search
    "tlsh_trie",          # TernaryTrie lookup
)


def tag_retrieval_path(fact_dict: dict, path: str) -> dict:
    """Add retrieval_path to a fact's provenance for audit trail."""
    if "provenance" not in fact_dict or fact_dict["provenance"] is None:
        fact_dict["provenance"] = {}
    fact_dict["provenance"]["retrieval_path"] = path
    return fact_dict


__all__ = [
    "ProtoDashAttributer",
    "sentence_level_score",
    "tag_retrieval_path",
    "RETRIEVAL_PATHS",
]
