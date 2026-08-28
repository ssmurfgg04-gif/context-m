"""Per-user idiolect normalization via embedding neighborhoods.

Self-supervised: builds a per-user idiolect centroid (EMA of recent
chunk embeddings) and an in-vocabulary codebook. When a noisy token
appears, looks up k nearest in-vocab neighbors weighted by idiolect
consistency, returns the canonical form if similarity >= threshold.

arxiv research: Göker 2018 (Turkish social media), TERUN 2020 (Roman
Hindi), Rocca & Weston 2022 (per-user transformers). The embedding-
neighborhood method is fully unsupervised.

Pure numpy. No trained model. Plays well with μ=0 — the
HashingEmbedder is deterministic.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Iterable

import numpy as np

from context_m.text.tokenizer import STOPWORDS, words


class PerUserIdiolectNormalizer:
    """Per-user idiolect-aware normalization.

    Maintains:
      * in-vocab codebook: canonical_token → embedding
      * per-user centroid: EMA of recent chunk embeddings

    On normalize(user, text):
      for each token, if in-vocab → leave alone; else look up k-NN in
      vocab weighted by (0.7 * direct_sim + 0.3 * user_centroid_sim),
      replace if best >= threshold.
    """

    def __init__(self, embedder, vocab_cap: int = 50_000,
                 win: int = 256, decay: float = 0.92,
                 min_count: int = 2, threshold: float = 0.78,
                 k: int = 5) -> None:
        self.embedder = embedder
        self.vocab_cap = vocab_cap
        self.win = win
        self.decay = decay
        self.min_count = min_count  # min user co-occurrences before promotion
        self.threshold = threshold
        self.k = k
        # canonical vocab: token → (embedding, count)
        self._vocab: "OrderedDict[str, tuple[np.ndarray, int]]" = OrderedDict()
        # user_id → (centroid_emb, raw_count)
        self._users: dict[str, tuple[np.ndarray, int]] = {}
        # (user_id, slang_token, canonical_token) → co-occurrence count
        self._co_counts: dict[tuple, int] = {}

    # ------------------------------------------------------- observation
    def observe(self, user_id: str, text: str) -> None:
        """Update the user's idiolect centroid and add tokens to vocab."""
        if not text or not text.strip():
            return
        v = self.embedder.embed(text)
        u = self._users.get(user_id)
        if u is None:
            self._users[user_id] = (v, 1)
        else:
            old, n = u
            new = self.decay * old + (1.0 - self.decay) * v
            nn = float(np.linalg.norm(new))
            self._users[user_id] = (new / nn if nn > 0 else new, n + 1)
        # add canonical tokens to vocab
        for tok in words(text):
            if tok in STOPWORDS or len(tok) < 2:
                continue
            if tok in self._vocab:
                emb, cnt = self._vocab[tok]
                self._vocab[tok] = (emb, cnt + 1)
            elif len(self._vocab) < self.vocab_cap:
                self._vocab[tok] = (self.embedder.embed(tok), 1)
        # LRU eviction
        if len(self._vocab) > self.vocab_cap:
            # drop least-recently-promoted entries
            while len(self._vocab) > self.vocab_cap:
                self._vocab.popitem(last=False)

    def observe_pair(self, user_id: str, slang: str, canonical: str) -> None:
        """Promote a slang→canonical mapping after multiple confirmations."""
        key = (user_id, slang.lower(), canonical.lower())
        self._co_counts[key] = self._co_counts.get(key, 0) + 1

    # ------------------------------------------------------- normalization
    def normalize_token(self, user_id: str, token: str) -> str:
        """Return canonical form of token for this user.

        Case-preserving: if the token is in vocab (case-insensitive),
        return the ORIGINAL token unchanged. Only normalize tokens
        that are genuinely OOV (no case-insensitive match).
        """
        if not token or token in STOPWORDS:
            return token
        # case-insensitive vocab check — preserves the original token
        # when it's already a canonical form (just with different case)
        token_lower = token.lower()
        if token_lower in self._vocab:
            return token  # already canonical, just preserve case
        # check if user has a promoted mapping
        canonical = self._find_promoted(user_id, token)
        if canonical is not None:
            return canonical
        # embedding k-NN over vocab
        if len(self._vocab) < 5:
            return token
        q = self.embedder.embed(token)
        ids = list(self._vocab.keys())
        embs = np.stack([self._vocab[i][0] for i in ids])
        sims = embs @ q  # (N,)
        # idiolect bias
        u = self._users.get(user_id)
        if u is not None:
            u_centroid = u[0]
            u_sims = embs @ u_centroid
            sims = 0.7 * sims + 0.3 * u_sims
        order = np.argsort(-sims)[: self.k]
        best_idx = int(order[0])
        if sims[best_idx] < self.threshold:
            return token  # OOV — preserve original
        return ids[best_idx]  # might be lowercased canonical

    def _find_promoted(self, user_id: str, token: str) -> str | None:
        """Return promoted canonical if (user, slang) co-occurred >= min_count."""
        token_l = token.lower()
        for (u, slang, canonical), cnt in self._co_counts.items():
            if u == user_id and slang == token_l and cnt >= self.min_count:
                return canonical
        return None

    def normalize(self, user_id: str, text: str) -> str:
        """Normalize all tokens in text per-user."""
        if not text:
            return text
        out = []
        for tok in text.split():
            # preserve simple punctuation
            prefix = ""
            core = tok
            while core and not core[0].isalnum():
                prefix += core[0]
                core = core[1:]
            suffix = ""
            while core and not core[-1].isalnum():
                suffix = core[-1] + suffix
                core = core[:-1]
            if core:
                canon = self.normalize_token(user_id, core)
                out.append(prefix + canon + suffix)
            else:
                out.append(tok)
        return " ".join(out)

    # ------------------------------------------------------- admin
    def stats(self) -> dict:
        return {
            "vocab_size": len(self._vocab),
            "users": len(self._users),
            "promoted_mappings": sum(1 for v in self._co_counts.values()
                                      if v >= self.min_count),
            "vocab_cap": self.vocab_cap,
            "threshold": self.threshold,
        }

    def save_state(self) -> dict:
        """Serialize for federation (deterministic)."""
        return {
            "vocab": [(k, v[0].tolist(), v[1]) for k, v in self._vocab.items()],
            "users": {k: [v[0].tolist(), v[1]] for k, v in self._users.items()},
            "co_counts": dict(self._co_counts),
        }

    def load_state(self, state: dict) -> None:
        self._vocab.clear()
        for k, emb, cnt in state.get("vocab", []):
            self._vocab[k] = (np.asarray(emb, dtype=np.float32), cnt)
        self._users = {k: (np.asarray(v[0], dtype=np.float32), v[1])
                       for k, v in state.get("users", {}).items()}
        self._co_counts = {tuple(k) if isinstance(k, list) else k: v
                           for k, v in state.get("co_counts", {}).items()}


__all__ = ["PerUserIdiolectNormalizer"]
