"""Deterministic feature-hashing embedder (μ=0 even at the embedding layer).

No external model, no API call, fully reproducible from a seed. Produces
L2-normalized float32 vectors of ``dims`` dimensions from token unigrams,
token bigrams and character n-grams via signed feature hashing
(Weinberger et al., 2009). A ``EmbeddingProvider`` protocol allows
swapping in a local transformer (e.g. ONNX MiniLM) or an API-backed
model in production without touching the rest of the fabric.
"""

from __future__ import annotations

import math
from typing import Protocol

import numpy as np

from context_m.text.tokenizer import STOPWORDS, words
from context_m.util import h64 as _h64


class EmbeddingProvider(Protocol):
    dims: int

    def embed(self, text: str) -> np.ndarray: ...

    def embed_many(self, texts: list[str]) -> np.ndarray: ...


def _h64(feature: str, seed: int) -> int:
    import hashlib
    return int.from_bytes(
        hashlib.blake2b(feature.encode("utf-8"), digest_size=8,
                        key=seed.to_bytes(8, "little")).digest(), "little")


class HashingEmbedder:
    """Signed feature hashing over token/bigram/char-ngram features."""

    def __init__(self, dims: int = 768, seed: int = 0x0C0FFEE,
                 char_ngrams: tuple[int, ...] = (3, 4, 5),
                 use_bigrams: bool = True) -> None:
        self.dims = dims
        self.seed = seed & 0xFFFFFFFFFFFFFFFF
        self.char_ngrams = char_ngrams
        self.use_bigrams = use_bigrams
        self._feat_cache: dict[str, tuple[int, int, float]] = {}

    # -- feature extraction -------------------------------------------------

    def _feature(self, token: str) -> tuple[int, int, float]:
        hit = self._feat_cache.get(token)
        if hit is not None:
            return hit
        h = _h64(token, self.seed)
        idx = h % self.dims
        sign = 1 if (h >> 63) & 1 else -1
        base = 0.35 if token in STOPWORDS else 1.0
        out = (idx, sign, base)
        if len(self._feat_cache) < 500_000:
            self._feat_cache[token] = out
        return out

    def _char_features(self, token: str) -> list[tuple[int, int, float]]:
        padded = f"^{token}$"
        feats = []
        for n in self.char_ngrams:
            if len(padded) < n:
                feats.append((padded, 0.5))
                continue
            for i in range(len(padded) - n + 1):
                feats.append((padded[i:i + n], 0.5))
        out = []
        for gram, w in feats:
            h = _h64(gram, self.seed ^ 0xA5A5)
            out.append((h % self.dims, 1 if (h >> 63) & 1 else -1, w))
        return out

    # -- API ------------------------------------------------------------------

    def embed(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dims, dtype=np.float32)
        toks = words(text)
        if not toks:
            vec[0] = 1.0
            return vec
        counts: dict[str, float] = {}
        for t in toks:
            counts[t] = counts.get(t, 0.0) + 1.0
        for tok, tf in counts.items():
            idx, sign, base = self._feature(tok)
            vec[idx] += sign * base * (1.0 + math.log(tf))
            for cidx, csign, cw in self._char_features(tok):
                vec[cidx] += csign * cw * (1.0 + math.log(tf)) * 0.35
        if self.use_bigrams:
            for a, b in zip(toks, toks[1:]):
                if a in STOPWORDS and b in STOPWORDS:
                    continue
                idx, sign, base = self._feature(f"{a}_{b}")
                vec[idx] += sign * base * 0.7
        n = float(np.linalg.norm(vec))
        if n > 0:
            vec /= n
        return vec

    def embed_many(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dims), dtype=np.float32)
        return np.stack([self.embed(t) for t in texts])
