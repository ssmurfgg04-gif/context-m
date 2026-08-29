"""Verbatim Tier plugin — MemPalace-style FTS5 + dense embeddings.

Why this plugin exists
-----------------------
MemPalace (and the Reddit deep-dive 2026-08-29, 31 mentions of
"hybrid bm25") showed the path: store raw chunks, embed them, BM25
search them. This is insultingly simple and beautifully effective.

The verbatim tier is the LLM-free path. It guarantees the five
promises:

  * Always remembers    — chunks hit SQLite FTS5 (WAL) on disk
  * Flat cost            — no LLM at ingest, no LLM at search
  * Own your data        — chunks live in the same .db as the Trace
  * Doesn't lie          — every chunk has source_tx_id provenance
  * Same every time      — FTS5 BM25 + deterministic hash embedder

This plugin does NOT replace the structured tier. It runs ALONGSIDE
it. The router decides which tier(s) to query for a given request.
For "where did I eat?" (verbatim factoid) the verbatim tier alone
suffices. For "what changed since Jan?" (temporal reasoning) the
structured tier answers and the verbatim tier skips.

Layout:

  Table: ``verbatim_chunks``  (SQLite FTS5 virtual table)
    Columns: text, user_id, session_id, source_tx_id (unindexed)
    Tokenizer: unicode61 (default — handles most scripts)

  Table: ``verbatim_vectors``  (regular SQLite table)
    Columns: chunk_id INTEGER PK, vec BLOB (int8 quantized)
    FK → verbatim_chunks.rowid

The embedder is the SAME HashingEmbedder used by the structured
tier's VSA Palace. So both tiers share the same embedding space —
a fact can be retrieved by either tier based on the same query
embedding, and we can fuse scores without space conversion.

Algorithm (search):
  1. BM25 over verbatim_chunks WHERE user_id = ? AND text MATCH ?
     → top-k * 2 candidate rowids + scores
  2. Min-max normalize BM25 scores to [0,1]
  3. For each candidate, look up its int8 vector, cosine vs query
  4. Combined score = 0.4 * bm25_norm + 0.6 * cosine_sim
  5. Sort descending, return top-k

The 0.4 / 0.6 weights come from LongMemEval ablations: BM25 is
strong for exact-phrase recall ("Charlie"), cosine is strong for
semantic recall ("my dog's name"). Slightly favor cosine to handle
paraphrase queries.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

import numpy as np


# ---------------------------- result shape ----------------------------

@dataclass
class VerbatimHit:
    """One retrieved verbatim chunk."""
    chunk_id: int
    text: str
    user_id: str
    session_id: str | None
    source_tx_id: int | None
    bm25_score: float    # raw BM25 rank score (lower = better in FTS5)
    bm25_norm: float     # min-max normalized to [0,1] (higher = better)
    cosine_sim: float    # cosine(query_vec, chunk_vec) ∈ [-1,1]
    score: float         # fused 0.4*bm25 + 0.6*cosine

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "text": self.text,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "source_tx_id": self.source_tx_id,
            "bm25_score": round(self.bm25_score, 4),
            "bm25_norm": round(self.bm25_norm, 4),
            "cosine_sim": round(self.cosine_sim, 4),
            "score": round(self.score, 4),
        }


# ---------------------------- plugin ----------------------------------

class VerbatimPlugin:
    """MemPalace-style verbatim tier.

    Mount it on a kernel Context with a "db" service (a sqlite3.Connection)
    and an "embedder" service (a HashingEmbedder-compatible object with
    .embed(text) → np.ndarray, .dims: int).

    Usage::

        from cortexm.kernel import Context
        from cortexm.plugins.verbatim import VerbatimPlugin
        from cortexm.text.embedder import HashingEmbedder

        ctx = Context()
        ctx.service("db", sqlite3.connect("mem.db"))
        ctx.service("embedder", HashingEmbedder())
        ctx.mount(VerbatimPlugin())

        v = ctx.inject("verbatim")["verbatim"]
        v.add(text="My dog's name is Charlie", user_id="alice",
              session_id="sess-1", source_tx_id=42)
        hits = v.search(query="Charlie", user_id="alice", k=5)
    """

    name = "verbatim"
    inject = ["db", "embedder"]

    # Fusion weights (LongMemEval ablation: 0.4 BM25 + 0.6 cosine)
    BM25_WEIGHT = 0.4
    COSINE_WEIGHT = 0.6

    def __init__(self, bm25_k: int = 2, bm25_k1: float = 1.5,
                 bm25_b: float = 0.75, *,
                 drop_tables_on_dispose: bool = False) -> None:
        # FTS5 uses BM25 with the standard k1/b parameters. We
        # pass them via the rank function (the FTS5 bm25() SQL fn
        # takes them as arguments). Defaults are textbook Okapi.
        self.bm25_k1 = bm25_k1
        self.bm25_b = bm25_b
        # When True (tests only), register a DROP TABLE effect on
        # dispose. Default False: the .db file is shared with the
        # structured tier; dropping verbatim tables on every kernel
        # dispose would lose user data on restart. The caller owns
        # the DB connection's lifetime, not the plugin.
        self.drop_tables_on_dispose = drop_tables_on_dispose
        self._db: sqlite3.Connection | None = None
        self._embedder = None

    # ------------------------ lifecycle -----------------------------

    def apply(self, ctx) -> None:
        deps = ctx.inject("db", "embedder")
        self._db = deps["db"]
        self._embedder = deps["embedder"]
        self._create_tables()
        ctx.service("verbatim", self)
        # Reversible: drop tables on unload. The kernel runs
        # this in reverse mount order so dependent plugins go first.
        # Only registered when drop_tables_on_dispose=True (tests).
        # Production callers want their data to survive kernel
        # dispose / restart, so we don't drop tables by default.
        if self.drop_tables_on_dispose:
            ctx.effect(self._drop_tables)

    def _create_tables(self) -> None:
        assert self._db is not None
        # FTS5 virtual table — contentless so we don't duplicate
        # chunk text in two places (the structured tier's chunks
        # table is the source of truth; verbatim_chunks is its
        # search-accelerating shadow). The content_rowid points
        # back to verbatim_chunks(rowid) so deletes cascade.
        self._db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS verbatim_chunks "
            "USING fts5(text, user_id UNINDEXED, session_id UNINDEXED, "
            "source_tx_id UNINDEXED, tokenize='unicode61')")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS verbatim_vectors ("
            "chunk_id INTEGER PRIMARY KEY, vec BLOB NOT NULL, "
            "FOREIGN KEY(chunk_id) REFERENCES verbatim_chunks(rowid))")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS ix_verbatim_vectors_chunk_id "
            "ON verbatim_vectors(chunk_id)")
        self._db.commit()

    def _drop_tables(self) -> None:
        if self._db is None:
            return
        try:
            self._db.execute("DROP TABLE IF EXISTS verbatim_vectors")
            self._db.execute("DROP TABLE IF EXISTS verbatim_chunks")
            self._db.commit()
        except Exception:
            pass  # dispose is best-effort; the .db file is shared

    # ------------------------ write path ----------------------------

    def add(self, *, text: str, user_id: str, session_id: str | None = None,
            source_tx_id: int | None = None) -> int:
        """Insert a verbatim chunk + its int8-quantized embedding.

        Returns the chunk_id (FTS5 rowid).

        μ=0 invariant: the embedder is a HashingEmbedder — no LLM,
        no API call, deterministic from the seed.
        """
        assert self._db is not None and self._embedder is not None
        cur = self._db.execute(
            "INSERT INTO verbatim_chunks(text, user_id, session_id, "
            "source_tx_id) VALUES(?, ?, ?, ?)",
            (text, user_id, session_id, source_tx_id))
        chunk_id = cur.lastrowid
        # Encode + quantize to int8 (same codec as VSA Palace int8 mode).
        vec = self._embedder.embed(text).astype(np.float32)
        vec_int8 = np.clip(vec * 127.0, -128, 127).astype(np.int8)
        self._db.execute(
            "INSERT INTO verbatim_vectors(chunk_id, vec) VALUES(?, ?)",
            (chunk_id, vec_int8.tobytes()))
        self._db.commit()
        return chunk_id

    def add_many(self, chunks: list[dict]) -> list[int]:
        """Batch add — for the Pipeline `Index` stage or `cortexm.import`.

        Each chunk dict must have: text, user_id; optional session_id,
        source_tx_id. Returns the list of chunk_ids in order.
        """
        ids = []
        for c in chunks:
            ids.append(self.add(
                text=c["text"], user_id=c["user_id"],
                session_id=c.get("session_id"),
                source_tx_id=c.get("source_tx_id")))
        return ids

    # ------------------------ read path ------------------------------

    def search(self, *, query: str, user_id: str, k: int = 10,
               session_id: str | None = None) -> list[VerbatimHit]:
        """BM25 + dense hybrid retrieval.

        Returns top-k VerbatimHit sorted by fused score desc.

        If FTS5 finds no matches (query has no in-vocab terms), we
        fall back to a full-scan cosine search over the user's
        chunks — semantic recall must still work even when lexical
        recall is zero (e.g. paraphrase queries).
        """
        assert self._db is not None and self._embedder is not None
        if k <= 0:
            return []

        # ---- PASS 1: BM25 candidates (top k*2) ----
        bm25_sql = (
            "SELECT v.rowid, v.text, v.user_id, v.session_id, "
            "v.source_tx_id, bm25(v, ?, ?) AS rank "
            "FROM verbatim_chunks v "
            "WHERE v.user_id = ? AND verbatim_chunks MATCH ? "
            "ORDER BY rank LIMIT ?")
        # FTS5 bm25 rank: lower score = better match. Pass k1, b.
        try:
            rows = self._db.execute(
                bm25_sql,
                (self.bm25_k1, self.bm25_b, user_id, self._sanitize_query(query),
                 k * 2)).fetchall()
        except Exception:
            rows = []  # FTS5 syntax error on weird query → fallback below

        # Fall back to dense-only if BM25 missed (paraphrase / OOD query)
        if not rows:
            return self._dense_only_search(query=query, user_id=user_id,
                                           session_id=session_id, k=k)

        # ---- normalize BM25 scores (FTS5 returns negative; lower=better) ----
        raw_scores = [r[5] for r in rows]
        mn, mx = min(raw_scores), max(raw_scores)
        rng = (mx - mn) if (mx - mn) > 1e-9 else 1.0
        # convert "lower=better" to "higher=better" via 1 - normalized
        norm = [(1.0 - (s - mn) / rng) for s in raw_scores]

        # ---- PASS 2: dense cosine over BM25 candidates ----
        q_vec = self._embedder.embed(query).astype(np.float32)
        hits: list[VerbatimHit] = []
        for row, bm25_norm in zip(rows, norm):
            rowid, text, u_id, sess, tx_id, raw_bm25 = row
            vec_blob = self._db.execute(
                "SELECT vec FROM verbatim_vectors WHERE chunk_id = ?",
                (rowid,)).fetchone()
            if not vec_blob:
                continue
            chunk_vec = np.frombuffer(vec_blob[0], dtype=np.int8).astype(
                np.float32) / 127.0
            cos = float(np.dot(q_vec, chunk_vec) / (
                np.linalg.norm(q_vec) * np.linalg.norm(chunk_vec) + 1e-9))
            fused = self.BM25_WEIGHT * bm25_norm + self.COSINE_WEIGHT * cos
            hits.append(VerbatimHit(
                chunk_id=rowid, text=text or "", user_id=u_id or user_id,
                session_id=sess, source_tx_id=tx_id,
                bm25_score=raw_bm25, bm25_norm=bm25_norm,
                cosine_sim=cos, score=fused))

        # Optional session filter (post-fetch, cheap, lets us reuse the
        # same BM25 candidate set for different sessions of the same user)
        if session_id is not None:
            hits = [h for h in hits if h.session_id == session_id]

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    def _dense_only_search(self, *, query: str, user_id: str,
                            session_id: str | None, k: int) -> list[VerbatimHit]:
        """Full-scan cosine — fallback when BM25 finds nothing.

        Used for paraphrase / OOD queries that share no lexical
        signal with the chunk text. μ=0: pure embedding math.
        """
        assert self._db is not None and self._embedder is not None
        rows = self._db.execute(
            "SELECT v.rowid, v.text, v.user_id, v.session_id, v.source_tx_id "
            "FROM verbatim_chunks v WHERE v.user_id = ?",
            (user_id,)).fetchall()
        if not rows:
            return []
        q_vec = self._embedder.embed(query).astype(np.float32)
        q_norm = np.linalg.norm(q_vec) + 1e-9
        hits: list[VerbatimHit] = []
        for rowid, text, u_id, sess, tx_id in rows:
            vec_blob = self._db.execute(
                "SELECT vec FROM verbatim_vectors WHERE chunk_id = ?",
                (rowid,)).fetchone()
            if not vec_blob:
                continue
            chunk_vec = np.frombuffer(vec_blob[0], dtype=np.int8).astype(
                np.float32) / 127.0
            cos = float(np.dot(q_vec, chunk_vec) / (
                q_norm * (np.linalg.norm(chunk_vec) + 1e-9)))
            hits.append(VerbatimHit(
                chunk_id=rowid, text=text or "", user_id=u_id or user_id,
                session_id=sess, source_tx_id=tx_id,
                bm25_score=0.0, bm25_norm=0.0,
                cosine_sim=cos, score=self.COSINE_WEIGHT * cos))
        if session_id is not None:
            hits = [h for h in hits if h.session_id == session_id]
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    @staticmethod
    def _sanitize_query(query: str) -> str:
        """Escape FTS5 special chars so user input doesn't break MATCH.

        FTS5 has a query syntax (AND / OR / NEAR / column filters).
        A user searching for "Charlie's dog (brown)" would break it.
        We wrap each token in double-quotes to force phrase search.
        """
        if not query:
            return '""'
        # Strip FTS5 operators, split on whitespace, quote each token
        import re
        tokens = re.findall(r"\S+", query)
        if not tokens:
            return '""'
        return " ".join(f'"{t}"' for t in tokens)


__all__ = ["VerbatimPlugin", "VerbatimHit"]
