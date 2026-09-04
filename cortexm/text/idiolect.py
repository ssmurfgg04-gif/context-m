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

from cortexm.text.tokenizer import STOPWORDS, words


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
        # canonical vocab: token → (embedding-or-None, count).  Most tokens
        # are immediately recognized as in-vocabulary on ingest, so eagerly
        # constructing a 768-dimensional embedding for each new token is
        # pure cost.  Materialize those vectors only if a genuine OOV lookup
        # needs the k-NN codebook.
        self._vocab: "OrderedDict[str, tuple[np.ndarray | None, int]]" = OrderedDict()
        self._vocab_ids: list[str] = []
        self._vocab_matrix: np.ndarray | None = None
        self._vocab_dirty = True
        # True when entries were REMOVED from the vocab (LRU eviction or
        # load_state) since the last matrix sync.  Removal invalidates
        # row alignment, so the matrix must be rebuilt from scratch;
        # without a removal, growth is append-only and the matrix can
        # be extended with only the new rows (O(delta) instead of O(n)).
        self._vocab_evicted = False
        # user_id → (centroid_emb, raw_count)
        self._users: dict[str, tuple[np.ndarray, int]] = {}
        # (user_id, slang_token, canonical_token) → co-occurrence count
        self._co_counts: dict[tuple, int] = {}
        # built-in text-speak escape hatch — a small curated map of
        # common short-forms that the embedding-kNN path can't recover
        # (because "u" / "ur" / "@" / "2" / "4" have very different
        # char n-gram signatures from their canonical forms). This is
        # reasonable because text-speak is a well-documented cross-user
        # idiolect; the normalizer uses it as a pre-learned baseline
        # before self-supervising per-user slang not in this map.
        # Public so callers can extend or override per domain.
        self.text_speak_map: dict[str, str] = {
            "u": "you", "ur": "your", "u r": "you are",
            "2": "to", "4": "for",
            "b4": "before", "tmr": "tomorrow", "defo": "definitely",
            "prolly": "probably", "kinda": "kind of", "sorta": "sort of",
            "gimme": "give me", "lemme": "let me",
            "wanna": "want to", "gonna": "going to",
            "gotta": "got to", "outta": "out of",
            "bc": "because", "dk": "don't know", "idk": "i don't know",
            "rn": "right now", "w/": "with", "w/o": "without",
            "ppl": "people", "thx": "thanks", "k": "okay",
            "rly": "really", "tho": "though",
            "@": "at", "&": "and", "b/c": "because",
            "y": "why", "r": "are", "n": "and",
            "im": "i'm", "ive": "i've", "ill": "i'll",
            "wouldnt": "wouldn't", "shouldnt": "shouldn't",
            "couldnt": "couldn't", "dont": "don't",
            "cant": "can't", "wont": "won't", "isnt": "isn't",
            "wasnt": "wasn't", "didnt": "didn't",
            "hasnt": "hasn't", "havent": "haven't",
        }

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
                self._vocab[tok] = (None, 1)
                self._vocab_dirty = True
        # LRU eviction
        if len(self._vocab) > self.vocab_cap:
            # drop least-recently-promoted entries
            while len(self._vocab) > self.vocab_cap:
                self._vocab.popitem(last=False)
            self._vocab_dirty = True
            self._vocab_evicted = True

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
        # text-speak escape hatch — checked FIRST because the embedding
        # kNN path can't recover "u"/"@"/"2" → canonical (their char
        # n-gram signatures are too different). The map is curated and
        # public so callers can extend per domain.
        token_lower = token.lower()
        if token_lower in self.text_speak_map:
            canon = self.text_speak_map[token_lower]
            if token[:1].isupper():
                canon = canon[:1].upper() + canon[1:]
            return canon
        # case-insensitive vocab check — preserves the original token
        # when it's already a canonical form (just with different case)
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
        if self._vocab_dirty or self._vocab_matrix is None or len(self._vocab_ids) != len(self._vocab):
            self._sync_vocab_matrix()
        ids = self._vocab_ids
        embs = self._vocab_matrix
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

    def _sync_vocab_matrix(self) -> None:
        """Re-materialize the k-NN codebook matrix — incrementally when
        possible.

        The vocab is an ``OrderedDict``: assignments to EXISTING keys
        preserve position and new keys append at the end, so when nothing
        was evicted since the last sync the old ``_vocab_ids`` are still
        a prefix of the current key order and the matrix only needs the
        new tail rows concatenated (O(delta)).  A full O(n) rebuild is
        reserved for the first build, LRU eviction, and load_state.

        This is what keeps sustained ingest linear: previously every
        message containing a new token triggered a full ``np.stack``
        over the entire (up to 50k x dims) vocabulary, making ingest
        quadratic in messages (measured: 20k msgs projected > 9 min).
        """
        ids = list(self._vocab.keys())
        n = len(ids)
        old_n = len(self._vocab_ids)
        if (self._vocab_matrix is not None and self._vocab_ids
                and not self._vocab_evicted and n > old_n):
            # append-only growth: embed only the new tail tokens
            tail_ids = ids[old_n:]
            rows: list[np.ndarray] = []
            for vocab_token in tail_ids:
                emb, count = self._vocab[vocab_token]
                if emb is None:
                    emb = self.embedder.embed(vocab_token)
                    self._vocab[vocab_token] = (emb, count)
                rows.append(emb)
            if rows:
                block = (np.stack(rows) if len(rows) > 1
                         else np.asarray(rows[0], dtype=rows[0].dtype)[None, :])
                self._vocab_matrix = np.concatenate(
                    [self._vocab_matrix, block], axis=0)
            self._vocab_ids = ids
        else:
            # full rebuild: first build, eviction, or load_state
            materialized: list[np.ndarray] = []
            for vocab_token in ids:
                emb, count = self._vocab[vocab_token]
                if emb is None:
                    emb = self.embedder.embed(vocab_token)
                    self._vocab[vocab_token] = (emb, count)
                materialized.append(emb)
            self._vocab_matrix = np.stack(materialized) if materialized else None
            self._vocab_ids = ids
            self._vocab_evicted = False
        self._vocab_dirty = False

    def _find_promoted(self, user_id: str, token: str) -> str | None:
        """Return promoted canonical if (user, slang) co-occurred >= min_count."""
        token_l = token.lower()
        for (u, slang, canonical), cnt in self._co_counts.items():
            if u == user_id and slang == token_l and cnt >= self.min_count:
                return canonical
        return None

    def normalize(self, user_id: str, text: str) -> str:
        """Normalize all tokens in text per-user.

        Preserves whitespace (including newlines) so downstream regex
        patterns that use ^ to anchor line starts still work — the
        text-speak map collapses multi-char tokens (u→you, 2→to) but
        shouldn't collapse newlines into spaces.
        """
        if not text:
            return text
        import re as _re
        out = []
        # use re.split with capture to preserve whitespace
        # tokens: runs of non-whitespace
        for piece in _re.split(r"(\s+)", text):
            if not piece:
                continue
            if piece.isspace():
                out.append(piece)
                continue
            tok = piece
            # CRITICAL: check the text-speak map for the FULL token
            # (with punctuation) BEFORE stripping — otherwise "@" gets
            # stripped to "" and never reaches the map. This is what
            # let "I work @ Microsoft" through un-normalized.
            tok_lower = tok.lower()
            if tok_lower in self.text_speak_map:
                canon = self.text_speak_map[tok_lower]
                if tok[:1].isupper():
                    canon = canon[:1].upper() + canon[1:]
                out.append(canon)
                continue
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
        return "".join(out)

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
        # State transfer needs concrete vectors.  This is deliberately the
        # only bulk materialization path; normal ingestion only needs token
        # membership until an OOV k-NN query occurs.
        vocab = []
        for token, (emb, count) in self._vocab.items():
            if emb is None:
                emb = self.embedder.embed(token)
                self._vocab[token] = (emb, count)
            vocab.append((token, emb.tolist(), count))
        return {
            "vocab": vocab,
            "users": {k: [v[0].tolist(), v[1]] for k, v in self._users.items()},
            "co_counts": dict(self._co_counts),
        }

    def load_state(self, state: dict) -> None:
        self._vocab.clear()
        for k, emb, cnt in state.get("vocab", []):
            self._vocab[k] = (np.asarray(emb, dtype=np.float32), cnt)
        self._vocab_ids = []
        self._vocab_matrix = None
        self._vocab_dirty = True
        self._vocab_evicted = True  # force full rebuild on next sync
        self._users = {k: (np.asarray(v[0], dtype=np.float32), v[1])
                       for k, v in state.get("users", {}).items()}
        self._co_counts = {tuple(k) if isinstance(k, list) else k: v
                           for k, v in state.get("co_counts", {}).items()}


__all__ = ["PerUserIdiolectNormalizer"]
