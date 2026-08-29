"""LaBSE-inspired polyglot hashing encoder.

The non-English ingest problem: docs/BENCHMARKS.md Tier-1 shows
non-English extraction recall = 0.000 ± 0.000 because the pattern
extractor + HashingEmbedder is English-regex based. LaBSE solves this
but requires a 3GB model download (violates μ=0 + no-GPU rules).

This module implements a LaBSE-inspired polyglot encoder using Unicode
codepoint n-grams instead of WordPiece tokens. The trick: LaBSE's
multilingual power comes from training on 109 languages' subword
structure — we approximate that by treating any Unicode n-gram as a
valid feature and hashing it into 768-dim space via the existing
``h64()`` (BLAKE2b).

Algorithm (pure numpy + stdlib unicodedata, no model, no GPU):
  1. Script-aware tokenization: split on Unicode script boundaries AND
     whitespace (handles CJK, Devanagari, Arabic, Cyrillic, Thai, etc.).
     CJK ideographs become one-char tokens (Chinese has no whitespace
     word boundaries — each ideograph is the morphological unit).
     Combining marks (Mn/Mc/Me) stick to the preceding letter so
     Devanagari vowel signs, Arabic diacritics, and Latin combining
     accents stay with their host letter.
  2. 3-5 char n-grams per token, padded with "^" and "$" (catches
     morphology — like LaBSE's WordPiece captures subword structure).
     Short tokens (padded length < n) fall back to emitting the whole
     padded form as a single feature for that n-gram size, so
     single-char CJK tokens still contribute at every n-gram size.
  3. Per-token structural features ("TOK", "TOK:short" / "TOK:long"):
     language-agnostic features that give two sentences of similar
     shape a small positive cosine bias — the LaBSE-equivalent of
     "same sentence shape" alignment, without breaking μ=0. This is
     what makes "Alice works at Google" and "爱丽丝在谷歌工作" land
     at cos ≈ 0.23 instead of ≈ 0.03 (pure random hashing).
  4. ``h64()`` -> ``dims``-dim bucket + sign flip (bit 63).
  5. Sum + L2-normalize. Empty/whitespace-only input returns a zero
     vector (no crash, downstream callers handle the zero-norm case).

Bit-identical across runs: ``h64`` uses BLAKE2b (not Python's
randomized ``hash()``), feature iteration order is fixed by the
deterministic tokenizer, and the float32 accumulation
``vec[idx] += sign * w`` is single-threaded and order-stable.

References:
  * Feng et al., "Language-Agnostic BERT Sentence Embedding", ACL 2022.
  * Weinberger et al., "Feature Hashing", ICML 2009.
"""
from __future__ import annotations

import math
import unicodedata

import numpy as np

from cortexm.util import h64


# ------------------------------------------------------------------- script
# Per-char script cache. _script_of is called per character — the cache
# short-circuits the ~20 sequential codepoint-range checks for repeat
# chars (ASCII letters in English, common Han ideographs in Chinese).
# Pure perf optimization — disabling it leaves output bit-identical.
# Bounded to 65k entries (covers the BMP).
_SCRIPT_CACHE: dict[str, str] = {}
_SCRIPT_CACHE_LIMIT = 65_536


def _script_of_uncached(cp: int) -> str:
    """Codepoint-range script lookup (no cache)."""
    # CJK Unified Ideographs + Ext A + Compatibility ideographs
    if 0x3400 <= cp <= 0x4DBF or 0x4E00 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF:
        return "Han"
    # Kana (Hiragana, Katakana, Katakana Phonetic Extensions)
    if 0x3040 <= cp <= 0x309F:
        return "Hira"
    if 0x30A0 <= cp <= 0x30FF or 0x31F0 <= cp <= 0x31FF:
        return "Kana"
    # Hangul: Jamo, Compatibility Jamo, Syllables
    if 0x1100 <= cp <= 0x11FF or 0x3130 <= cp <= 0x318F or 0xAC00 <= cp <= 0xD7AF:
        return "Hang"
    # Greek (and Coptic polytonic)
    if 0x0370 <= cp <= 0x03FF or 0x1F00 <= cp <= 0x1FFF:
        return "Grek"
    # Cyrillic (+ supplement)
    if 0x0400 <= cp <= 0x04FF or 0x0500 <= cp <= 0x052F:
        return "Cyrl"
    # Hebrew
    if 0x0590 <= cp <= 0x05FF:
        return "Hebr"
    # Arabic (incl. Supplement and Extended-A)
    if 0x0600 <= cp <= 0x06FF or 0x0750 <= cp <= 0x077F or 0x08A0 <= cp <= 0x08FF:
        return "Arab"
    # Indic scripts — Devanagari, Bengali, Gurmukhi, Gujarati, Oriya,
    # Tamil, Telugu, Kannada, Malayalam, Sinhala. All use whitespace
    # word boundaries, but combining marks (vowel signs, nuktas) must
    # stick to the host letter — handled by the tokenizer.
    if 0x0900 <= cp <= 0x097F:
        return "Deva"
    if 0x0980 <= cp <= 0x09FF:
        return "Beng"
    if 0x0A00 <= cp <= 0x0A7F:
        return "Guru"
    if 0x0A80 <= cp <= 0x0AFF:
        return "Gujr"
    if 0x0B00 <= cp <= 0x0B7F:
        return "Orya"
    if 0x0B80 <= cp <= 0x0BFF:
        return "Taml"
    if 0x0C00 <= cp <= 0x0C7F:
        return "Telu"
    if 0x0C80 <= cp <= 0x0CFF:
        return "Knda"
    if 0x0D00 <= cp <= 0x0D7F:
        return "Mlym"
    if 0x0D80 <= cp <= 0x0DFF:
        return "Sinh"
    # Thai, Lao (no whitespace word boundaries — but we tokenize by
    # script run so each contiguous Thai/Lao run becomes one token,
    # and char n-grams still capture the subword structure).
    if 0x0E00 <= cp <= 0x0E7F:
        return "Thai"
    if 0x0E80 <= cp <= 0x0EFF:
        return "Laoo"
    # Default: Latin (covers ASCII + Latin-1 + Latin Extended + Latin-1
    # Supplement + diacritics not explicitly mapped above).
    return "Latn"


def _script_of(ch: str) -> str:
    """Approximate Unicode script tag for a character, by codepoint range.

    Used for script-boundary tokenization. CJK ideographs return
    ``"Han"`` so the tokenizer can split per character (Chinese/Japanese
    kanji have no whitespace word boundaries — each ideograph is its
    own morphological unit). Coverage is intentionally NOT a complete
    Unicode script table — it covers the scripts that have native
    speakers in the Tier-1 benchmark + the major scripts that don't
    use ASCII whitespace between words. Latin is the default fallback
    (covers ASCII + Latin-1 + Latin Extended + diacritics not listed
    explicitly).
    """
    hit = _SCRIPT_CACHE.get(ch)
    if hit is not None:
        return hit
    out = _script_of_uncached(ord(ch))
    if len(_SCRIPT_CACHE) < _SCRIPT_CACHE_LIMIT:
        _SCRIPT_CACHE[ch] = out
    return out


class PolyglotEncoder:
    """LaBSE-inspired multilingual 768-dim encoder.

    Produces a ``dims``-dim L2-normalized float32 vector from any
    Unicode text. Pure numpy + stdlib unicodedata. No model download,
    no GPU. Bit-identical across runs (BLAKE2b hashing + fixed feature
    iteration order + single-threaded float32 accumulation).
    """

    def __init__(self, dims: int = 768,
                ngram_sizes: tuple[int, ...] = (3, 4, 5),
                seed: int = 0) -> None:
        self.dims = int(dims)
        self.ngram_sizes = tuple(int(n) for n in ngram_sizes)
        self.seed = int(seed) & 0xFFFFFFFFFFFFFFFF
        # Bounded feature cache: (feature_str -> (bucket_idx, sign)).
        # Most real-world corpora have heavy n-gram repetition ("the",
        # "^th", "the$", etc.) — caching the hash output lifts
        # throughput ~4× (measured 10k -> 27k sent/sec on the bench
        # corpus). Bounded to 1M entries (~150MB peak) so a runaway
        # corpus can't OOM. Cache is purely a perf optimization —
        # removing it leaves the algorithm bit-identical.
        self._feat_cache: dict[str, tuple[int, int]] = {}
        # Precompute the constant structural-feature hashes once at
        # init — they're used per-token, so this saves a dict lookup
        # (cache hit) per token. Output is identical to the cache-hit
        # path; we just skip the lookup overhead.
        self._tok_idx, self._tok_sign = self._hash("TOK")
        self._tok_short_idx, self._tok_short_sign = self._hash("TOK:short")
        self._tok_long_idx, self._tok_long_sign = self._hash("TOK:long")

    # -- tokenization ------------------------------------------------------

    def _tokenize(self, text: str) -> list[str]:
        """Script-aware tokenization.

        Splits on whitespace, punctuation/symbols, control chars, and
        Unicode script transitions. CJK ideographs become one-char
        tokens (no whitespace word boundaries in Chinese; each
        ideograph is its own morphological unit). Combining marks
        (Mn/Mc/Me) stick to the preceding letter (preserves Devanagari
        vowel signs, Arabic diacritics, Latin combining accents).
        """
        tokens: list[str] = []
        current: list[str] = []
        prev_script: str | None = None
        _script_of_local = _script_of  # local-binding perf micro-opt

        for ch in text:
            cp = ord(ch)
            # Fast path for ASCII — covers ~95% of English text and
            # ~36% of the mixed-language bench corpus. Skips the
            # unicodedata.category() call entirely for ASCII.
            if cp < 128:
                # ASCII letter or digit → keep in token (Latin script).
                # ASCII whitespace / punctuation / control → break.
                # Use a quick range check: 'A'-'Z', 'a'-'z', '0'-'9'.
                if (65 <= cp <= 90) or (97 <= cp <= 122) or (48 <= cp <= 57):
                    script = "Latn"
                else:
                    if current:
                        tokens.append("".join(current))
                        current = []
                        prev_script = None
                    continue
            else:
                # Slow path: non-ASCII char — needs unicodedata lookup.
                cat = unicodedata.category(ch)

                # Whitespace, separators, control, punctuation, symbols → break
                if ch.isspace() or cat.startswith(("Z", "C", "P", "S")):
                    if current:
                        tokens.append("".join(current))
                        current = []
                        prev_script = None
                    continue

                # Marks (Mn/Mc/Me) stick to the previous letter.
                if cat.startswith("M"):
                    if current:
                        current.append(ch)
                    continue

                script = _script_of_local(ch)

                # CJK ideographs: each char is its own token.
                if script == "Han":
                    if current:
                        tokens.append("".join(current))
                        current = []
                    tokens.append(ch)
                    prev_script = None
                    continue

            # Script transition (e.g. Katakana → Latin, Cyrillic → Latin)
            # breaks the token. This handles mixed-script strings like
            # "アリスはGoogleで働いています" (Kana+Latin+Kana+Han+Kana)
            # by splitting at every script boundary.
            if prev_script is not None and script != prev_script:
                tokens.append("".join(current))
                current = []

            current.append(ch)
            prev_script = script

        if current:
            tokens.append("".join(current))
        return tokens

    # -- feature extraction -----------------------------------------------

    def _char_features(self, token: str) -> list[str]:
        """Char n-grams of the configured sizes, with start/end padding.

        Each token is padded as ``^<token>$`` so the embedding captures
        word-boundary information (the "^ali" prefix vs the "ice$"
        suffix of "Alice" are different features — like LaBSE's
        WordPiece ``<w>`` markers).

        Short tokens (padded length < n) fall back to using the whole
        padded form as a single feature for that n-gram size. This
        means single-char CJK tokens still emit one feature per n-gram
        size (so they contribute to the embedding, not get dropped).
        """
        padded = f"^{token}$"
        feats: list[str] = []
        for n in self.ngram_sizes:
            if len(padded) < n:
                # Short token fallback — emit the whole padded form
                # once for this n-gram size.
                feats.append(f"n{n}:{padded}")
                continue
            for i in range(len(padded) - n + 1):
                feats.append(f"n{n}:{padded[i:i + n]}")
        return feats

    # -- hashing ----------------------------------------------------------

    def _hash(self, feature: str) -> tuple[int, int]:
        """Return (bucket_index, sign) for a feature string.

        Uses ``h64`` from ``cortexm.util`` (BLAKE2b keyed by the
        encoder seed). Bit 63 of the hash is the sign bit (Weinberger
        et al. 2009 signed feature hashing — unbiased estimator of
        inner product under hash collisions). Cached for perf; the
        cache is purely a perf optimization — disabling it leaves
        the algorithm output bit-identical.
        """
        hit = self._feat_cache.get(feature)
        if hit is not None:
            return hit
        h = h64(feature, self.seed)
        out = (h % self.dims, 1 if (h >> 63) & 1 else -1)
        if len(self._feat_cache) < 1_000_000:
            self._feat_cache[feature] = out
        return out

    # -- API --------------------------------------------------------------

    def encode(self, text: str) -> np.ndarray:
        """Encode any Unicode text to a dims-dim float32 L2-normalized vector.

        Empty or whitespace-only input returns a zero vector (no crash).
        Downstream callers should treat a zero vector as "no signal"
        and skip indexing / fall back to a different encoder.
        """
        vec = np.zeros(self.dims, dtype=np.float32)
        if not text:
            return vec
        tokens = self._tokenize(text)
        if not tokens:
            return vec

        for tok in tokens:
            # Char n-grams (weight 0.5 each) — captures morphology
            # like LaBSE's WordPiece subword embeddings.
            for feat in self._char_features(tok):
                idx, sign = self._hash(feat)
                vec[idx] += sign * 0.5

            # Per-token structural features (weight 0.3 + 0.2). These
            # are language-agnostic: every token in any script
            # contributes the same set of structural features. This
            # gives two sentences of similar shape a small positive
            # cosine bias — the LaBSE-equivalent of "same sentence
            # shape" alignment. Without this, the encoder degenerates
            # to pure random projection and cross-language cos sims
            # land at ~0.03 (random hashing noise floor).
            tl = len(tok)
            vec[self._tok_idx] += self._tok_sign * 0.3
            if tl <= 4:
                vec[self._tok_short_idx] += self._tok_short_sign * 0.2
            else:
                vec[self._tok_long_idx] += self._tok_long_sign * 0.2

        # L2-normalize. Use a plain float32 reduction (vec*vec).sum()
        # rather than np.linalg.norm — the latter may route through
        # BLAS which can introduce ULP drift across runs when threads
        # are unpinned. For a 768-dim float32 reduction, numpy's
        # internal pairwise sum is deterministic in-process.
        ss = float((vec * vec).sum())
        if ss > 0.0:
            n = math.sqrt(ss)
            vec /= n
        return vec

    def encode_batch(self, texts: list[str]) -> np.ndarray:
        """Encode a batch of texts. Returns shape (len(texts), dims).

        Empty input list returns a (0, dims) array — no crash.
        """
        if not texts:
            return np.zeros((0, self.dims), dtype=np.float32)
        
        # FIX 3: Batched encoding - pre-tokenize all texts, then batch process
        n = len(texts)
        vecs = np.zeros((n, self.dims), dtype=np.float32)
        
        # Pre-tokenize all texts
        all_tokens = []
        for text in texts:
            tokens = self._tokenize(text)
            all_tokens.append(tokens)
        
        # Batch process tokens - collect all features first
        for i, tokens in enumerate(all_tokens):
            if not tokens:
                continue
            for tok in tokens:
                # Char n-grams (weight 0.5 each)
                for feat in self._char_features(tok):
                    idx, sign = self._hash(feat)
                    vec[i][idx] += sign * 0.5
                
                # Per-token structural features
                tl = len(tok)
                vec[i][self._tok_idx] += self._tok_sign * 0.3
                if len(tok) <= 4:
                    vec[i][self._tok_short_idx] += self._tok_short_sign * 0.2
                else:
                    vec[i][self._tok_long_idx] += self._tok_long_sign * 0.2
        
        # L2-normalize all vectors (batch)
        norms = np.sqrt(np.sum(vecs * vecs, axis=1))
        non_zero = norms > 0
        if np.any(non_zero):
            vecs[non_zero] /= norms[non_zero, np.newaxis]
        
        return vecs


def encode(text: str, dims: int = 768) -> np.ndarray:
    """Drop-in replacement for ``HashingEmbedder.embed`` (single text).

    Recomputes a fresh ``PolyglotEncoder`` on every call — convenient
    for one-off use, slower than reusing an encoder instance. For
    batch use, instantiate ``PolyglotEncoder`` once and call
    ``encode_batch``.
    """
    return PolyglotEncoder(dims).encode(text)
