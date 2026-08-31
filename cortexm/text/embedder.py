"""Deterministic feature-hashing embedder (μ=0 even at the embedding layer).

No external model, no API call, fully reproducible from a seed. Produces
L2-normalized float32 vectors of ``dims`` dimensions from token unigrams,
token bigrams and character n-grams via signed feature hashing
(Weinberger et al., 2009). A ``EmbeddingProvider`` protocol allows
swapping in a local transformer (e.g. ONNX MiniLM) or an API-backed
model in production without touching the rest of the fabric.

With ``labse_enabled=True`` (Config.labse_enabled), text whose non-ASCII
ratio exceeds 30% is delegated to ``PolyglotEncoder`` — a LaBSE-inspired
Unicode n-gram hasher that handles CJK / Devanagari / Arabic / Cyrillic
scripts (which the regex tokenizer drops entirely, producing a constant
embedding and zero retrieval recall — see docs/BENCHMARKS.md Tier-1).
English text stays on the existing fast path. See
``context_m/text/labse.py`` for the polyglot algorithm.
"""

from __future__ import annotations

import math
from typing import Protocol

import numpy as np

from cortexm.text.labse import PolyglotEncoder
from cortexm.text.tokenizer import STOPWORDS, words
from cortexm.util import h64 as _h64


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
    """Signed feature hashing over token/bigram/char-ngram features.

    With ``labse_enabled=True``, non-English text (>30% non-ASCII chars)
    is delegated to ``PolyglotEncoder`` — a LaBSE-inspired Unicode
    n-gram encoder that handles scripts the regex tokenizer drops
    (CJK, Devanagari, Arabic, Cyrillic, Thai, Hangul, Kana). English
    text stays on the existing fast path. Default OFF so existing
    behavior is unchanged; opt in via ``Config.labse_enabled`` or the
    ``CONTEXT_M_LABSE`` env var.
    """

    def __init__(self, dims: int = 768, seed: int = 0x0C0FFEE,
                 char_ngrams: tuple[int, ...] = (3, 4, 5),
                 use_bigrams: bool = True,
                 labse_enabled: bool = False) -> None:
        self.dims = dims
        self.seed = seed & 0xFFFFFFFFFFFFFFFF
        self.char_ngrams = char_ngrams
        self.use_bigrams = use_bigrams
        self.labse_enabled = labse_enabled
        # LRU caches with size caps to prevent unbounded growth
        from functools import lru_cache
        self._feat_cache: dict[str, tuple[int, int, float]] = {}
        self._feat_cache_max = 500_000
        self._char_feat_cache: dict[str, tuple[tuple[int, int, float], ...]] = {}
        self._char_feat_cache_max = 200_000
        self._cache_hits = 0
        self._cache_misses = 0
        # Lazy-initialized polyglot encoder — only built when first needed
        # so the labse.py module import cost is paid only by users who
        # actually ingest non-English text.
        self._polyglot: PolyglotEncoder | None = None

    @property
    def polyglot(self) -> PolyglotEncoder:
        """Lazily-constructed PolyglotEncoder (dims/seed-matched)."""
        if self._polyglot is None:
            self._polyglot = PolyglotEncoder(
                dims=self.dims, seed=self.seed)
        return self._polyglot

    @staticmethod
    def _non_ascii_ratio(text: str) -> float:
        """Fraction of non-ASCII chars in text. 0.0 for empty input."""
        if not text:
            return 0.0
        non_ascii = sum(1 for c in text if ord(c) > 127)
        return non_ascii / len(text)

    # -- feature extraction -------------------------------------------------

    def _feature(self, token: str) -> tuple[int, int, float]:
        hit = self._feat_cache.get(token)
        if hit is not None:
            self._cache_hits += 1
            return hit
        self._cache_misses += 1
        h = _h64(token, self.seed)
        idx = h % self.dims
        sign = 1 if (h >> 63) & 1 else -1
        base = 0.35 if token in STOPWORDS else 1.0
        out = (idx, sign, base)
        # LRU eviction: drop 10% of cache when full
        if len(self._feat_cache) >= self._feat_cache_max:
            drop_n = self._feat_cache_max // 10
            for _k in list(self._feat_cache.keys())[:drop_n]:
                del self._feat_cache[_k]
        self._feat_cache[token] = out
        return out

    def _char_features(self, token: str) -> list[tuple[int, int, float]]:
        hit = self._char_feat_cache.get(token)
        if hit is not None:
            self._cache_hits += 1
            return list(hit)
        self._cache_misses += 1
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
        frozen = tuple(out)
        if len(self._char_feat_cache) < 500_000:
            self._char_feat_cache[token] = frozen
        return list(frozen)

    # -- API ------------------------------------------------------------------

    def embed(self, text: str) -> np.ndarray:
        # Polyglot fallback for non-English text — the regex tokenizer
        # in words() drops non-ASCII letters entirely, so without this
        # delegation, every non-English sentence embeds to the constant
        # [1, 0, 0, ...] vector (Tier-1 non-English recall = 0.000).
        if self.labse_enabled and self._non_ascii_ratio(text) > 0.30:
            return self.polyglot.encode(text)
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
        
        # FIX 3: Batched embedding - process all texts in a single pass
        # to avoid Python loop overhead and leverage numpy vectorization
        n = len(texts)
        vecs = np.zeros((n, self.dims), dtype=np.float32)
        
        # Pre-compute which texts go to polyglot vs standard path
        polyglot_indices = []
        standard_indices = []
        for i, text in enumerate(texts):
            if self.labse_enabled and self._non_ascii_ratio(text) > 0.30:
                polyglot_indices.append(i)
            else:
                standard_indices.append(i)
        
        # Batch process polyglot texts
        if polyglot_indices:
            poly_texts = [texts[i] for i in polyglot_indices]
            poly_vecs = self.polyglot.encode_many(poly_texts)
            for idx, vec in zip(polyglot_indices, poly_vecs):
                vecs[idx] = vec
        
        # Batch process standard texts - vectorized feature extraction
        if standard_indices:
            # Build all tokens and features in batch
            all_tokens = {}
            for i in standard_indices:
                toks = words(texts[i])
                for t in toks:
                    all_tokens[t] = True
            
            # Pre-compute all features
            token_features = {}
            for tok in all_tokens:
                token_features[tok] = self._feature(tok)
            
            # Build vectors in batch
            for i in standard_indices:
                vec = np.zeros(self.dims, dtype=np.float32)
                toks = words(texts[i])
                if not toks:
                    # v0.6.4: match embed()'s empty-token fallback —
                    # embed() returns [1, 0, 0, ...] (a non-zero
                    # sentinel that keeps cosine similarity well
                    # defined), but embed_many() returned a zero
                    # vector, so the same text embedded differently
                    # depending on the call path.
                    vec[0] = 1.0
                    vecs[i] = vec
                    continue
                counts = {}
                for t in toks:
                    counts[t] = counts.get(t, 0.0) + 1.0
                for tok, tf in counts.items():
                    idx, sign, base = token_features[tok]
                    vec[idx] += sign * base * (1.0 + math.log(tf))
                    for cidx, csign, cw in self._char_features(tok):
                        vec[cidx] += csign * cw * (1.0 + math.log(tf)) * 0.35
                if self.use_bigrams:
                    toks = words(texts[i])
                    for a, b in zip(toks, toks[1:]):
                        if a in STOPWORDS and b in STOPWORDS:
                            continue
                        idx, sign, base = self._feature(f"{a}_{b}")
                        vec[idx] += sign * base * 0.7
                n = float(np.linalg.norm(vec))
                if n > 0:
                    vec /= n
                vecs[i] = vec
        
        return vecs
