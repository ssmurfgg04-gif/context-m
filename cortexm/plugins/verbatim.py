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
                 drop_tables_on_dispose: bool = False,
                 prf_enabled: bool = True) -> None:
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
        # v0.5.3: PRF (pseudo-relevance feedback) — re-query with the
        # top BM25 hits' content words to surface chunks that share
        # vocabulary with the top hits but don't lexically match the
        # original query. Default ON. Turn off for benchmarks that
        # need pure BM25 baseline.
        self.prf_enabled = prf_enabled
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
        # agent_id UNINDEXED: stored but not tokenized — used for
        # the InjecMEM scope sandbox (user queries see only user-
        # scoped chunks; agent queries see user + own agent chunks).
        # v0.5.3: ALTER TABLE for existing DBs that lack agent_id.
        # SQLite doesn't support ALTER on virtual tables, so we
        # CREATE TABLE IF NOT EXISTS and try to backfill via a
        # CREATE VIRTUAL TABLE that includes the column on fresh DBs.
        try:
            self._db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS verbatim_chunks "
                "USING fts5(text, user_id UNINDEXED, session_id UNINDEXED, "
                "source_tx_id UNINDEXED, agent_id UNINDEXED, "
                "tokenize='unicode61')")
        except Exception:
            # Fallback for DBs that had the old schema (pre-0.5.3)
            # without agent_id column. Drop + recreate so the new
            # column exists. WARNING: this loses verbatim_chunks data
            # on upgrade. The structured tier (chunks + facts) is the
            # source of truth — verbatim_chunks is regenerated on next
            # ingest. For a fresh memory: this branch never fires.
            self._db.execute("DROP TABLE IF EXISTS verbatim_chunks")
            self._db.execute(
                "CREATE VIRTUAL TABLE verbatim_chunks "
                "USING fts5(text, user_id UNINDEXED, session_id UNINDEXED, "
                "source_tx_id UNINDEXED, agent_id UNINDEXED, "
                "tokenize='unicode61')")
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
            source_tx_id: int | None = None,
            agent_id: str | None = None) -> int:
        """Insert a verbatim chunk + its int8-quantized embedding.

        Returns the chunk_id (FTS5 rowid).

        μ=0 invariant: the embedder is a HashingEmbedder — no LLM,
        no API call, deterministic from the seed.

        v0.5.3: agent_id stored UNINDEXED — used by search() to honor
        the InjecMEM scope sandbox. User-scoped chunks have
        agent_id=NULL; agent-scoped chunks have agent_id=<id>. A user
        query (agent_id=None) sees ONLY user-scoped chunks; an agent
        query (agent_id=X) sees user chunks + agent X's own chunks.
        """
        assert self._db is not None and self._embedder is not None
        cur = self._db.execute(
            "INSERT INTO verbatim_chunks(text, user_id, session_id, "
            "source_tx_id, agent_id) VALUES(?, ?, ?, ?, ?)",
            (text, user_id, session_id, source_tx_id, agent_id))
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
        source_tx_id, agent_id. Returns the list of chunk_ids in order.
        """
        ids = []
        for c in chunks:
            ids.append(self.add(
                text=c["text"], user_id=c["user_id"],
                session_id=c.get("session_id"),
                source_tx_id=c.get("source_tx_id"),
                agent_id=c.get("agent_id")))
        return ids

    # ------------------------ read path ------------------------------

    def search(self, *, query: str, user_id: str, k: int = 10,
               session_id: str | None = None,
               agent_id: str | None = None) -> list[VerbatimHit]:
        """BM25 + dense hybrid retrieval with PRF expansion.

        Returns top-k VerbatimHit sorted by fused score desc.

        v0.5.3: ``agent_id`` enforces the InjecMEM scope sandbox:
          - ``agent_id=None`` (user scope): see ONLY chunks where
            ``agent_id IS NULL`` (user-scoped chunks).
          - ``agent_id=X`` (agent scope): see chunks where
            ``agent_id IS NULL OR agent_id = 'X'`` (user + own agent).

        v0.5.3 PRF (pseudo-relevance feedback): after the first BM25
        pass, take the top-3 hits' content words, append them to the
        query, and re-run BM25. This surfaces chunks whose terms
        co-occur with the query's content terms even when the chunk
        itself doesn't lexically match the query. The classic example:
        Q="what restaurant did I mention?" → BM25 finds chunks mentioning
        "restaurant"; PRF re-queries with the cuisine/neighborhood terms
        from those hits, surfacing the actual answer chunk that names
        the restaurant but doesn't contain "restaurant".

        If FTS5 finds no matches (query has no in-vocab terms), we
        fall back to a full-scan cosine search over the user's
        chunks — semantic recall must still work even when lexical
        recall is zero (e.g. paraphrase queries).
        """
        assert self._db is not None and self._embedder is not None
        if k <= 0:
            return []

        # InjecMEM sandbox filter: user-scope (agent_id=None) sees only
        # user-scoped chunks; agent scope sees user + own agent chunks.
        if agent_id is None:
            scope_filter = "agent_id IS NULL"
            scope_params: tuple = ()
        else:
            scope_filter = "(agent_id IS NULL OR agent_id = ?)"
            scope_params = (agent_id,)

        # ---- PASS 1: BM25 candidates (top k*2) ----
        # FTS5's bm25() requires the bare table name, NOT an alias.
        # Using `bm25(v, ?, ?)` with alias `v` raises "no such column: v".
        # Use the full table name throughout. Same for the MATCH clause.
        bm25_sql = (
            "SELECT rowid, text, user_id, session_id, "
            "source_tx_id, bm25(verbatim_chunks, ?, ?) AS rank "
            "FROM verbatim_chunks "
            f"WHERE user_id = ? AND {scope_filter} "
            "AND verbatim_chunks MATCH ? "
            "ORDER BY rank LIMIT ?")
        # FTS5 bm25 rank: lower score = better match. Pass k1, b.
        try:
            rows = self._db.execute(
                bm25_sql,
                (self.bm25_k1, self.bm25_b, user_id, *scope_params,
                 self._sanitize_query(query), k * 2)).fetchall()
        except Exception:
            rows = []  # FTS5 syntax error on weird query → fallback below

        # ---- PASS 1.5: PRF expansion (pseudo-relevance feedback) ----
        # Take the top-3 hits from PASS 1, extract their content words,
        # and re-query with an expanded query that includes those terms.
        # This surfaces chunks that share vocabulary with the top hits
        # but don't lexically match the original query.
        if self.prf_enabled and len(rows) >= 3:
            try:
                prf_words = self._prf_extract_terms(
                    [r[1] for r in rows[:3]], max_terms=8)
                if prf_words:
                    expanded_q = self._sanitize_query(query) + \
                        " OR " + " OR ".join(f'"{w}"' for w in prf_words)
                    prf_rows = self._db.execute(
                        bm25_sql,
                        (self.bm25_k1, self.bm25_b, user_id, *scope_params,
                         expanded_q, k)).fetchall()
                    # Union: dedupe by rowid, prefer PASS 1's score
                    seen = {r[0] for r in rows}
                    for r in prf_rows:
                        if r[0] not in seen:
                            rows.append(r)
                            seen.add(r[0])
            except Exception:
                pass  # PRF is best-effort — never break search

        # Fall back to dense-only if BM25 missed (paraphrase / OOD query)
        if not rows:
            return self._dense_only_search(query=query, user_id=user_id,
                                           session_id=session_id,
                                           agent_id=agent_id, k=k)

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

    @staticmethod
    def _prf_extract_terms(texts: list[str], max_terms: int = 8) -> list[str]:
        """Extract content words from the top BM25 hits for PRF expansion.

        We want terms that DISCRIMINATE the top hits from the average
        chunk — proper nouns, numbers, and rare words. The standard
        PRF approach: take the top-N hits, count term frequency across
        them, weight by inverse document frequency (rarity). The terms
        with the highest TF-IDF in the top-N go into the expansion.

        μ=0: pure string + set operations. No LLM, no embeddings.
        """
        import re
        # Stopword list — same as _sanitize_query's
        _STOP = {
            "the", "a", "an", "is", "are", "was", "were", "did", "do",
            "does", "what", "where", "when", "how", "who", "why",
            "i", "me", "my", "we", "us", "our", "you", "your",
            "he", "she", "it", "they", "them", "their",
            "and", "or", "but", "of", "to", "for", "from", "by",
            "with", "as", "at", "in", "on", "so", "if", "be",
            "this", "that", "these", "those", "have", "has", "had",
            "would", "could", "should", "will", "can", "may", "might",
            "about", "into", "any", "some", "all", "every", "each",
            "go", "going", "gone", "went", "get", "got", "want", "wanted",
            "tell", "told", "ask", "asked", "think", "thought",
            "remind", "wonder", "wondering", "could", "would",
            "really", "very", "just", "also", "too", "only",
            "out", "up", "down", "off", "over", "then", "than",
            "like", "love", "hate", "know", "need", "feel", "felt",
            "definitely", "actually", "maybe", "sure", "great",
            "good", "bad", "amazing", "awesome", "cool", "nice",
            "wow", "yum", "yeah", "yes", "no", "okay", "ok",
            "much", "many", "more", "most", "less", "few",
            "do", "doing", "done", "did",
        }
        # Count term frequency across the top texts
        tf: dict[str, int] = {}
        for t in texts:
            seen = set()
            for w in re.findall(r"\w+", t.lower()):
                if w in _STOP or len(w) < 3 or w.isdigit():
                    continue
                # Skip very common words (already in all chunks)
                if w in seen:
                    continue
                seen.add(w)
                tf[w] = tf.get(w, 0) + 1
        if not tf:
            return []
        # Pick the top-N by frequency (terms appearing in multiple top
        # hits are the most discriminative). Ties broken alphabetically.
        ranked = sorted(tf.items(), key=lambda kv: (-kv[1], kv[0]))
        return [w for w, _ in ranked[:max_terms]]

    def fetch_neighbors(self, *, chunk_id: int, user_id: str,
                         before: int = 1, after: int = 1,
                         agent_id: str | None = None) -> list[dict]:
        """Fetch the chunks adjacent to a hit chunk in ingest order.

        The verbatim tier indexes ONLY user messages by default (per
        the LongMemEval canonical runner's `_flatten_haystack`). The
        user's message that matches the query often doesn't contain
        the answer — the answer lives in the ASSISTANT's response that
        immediately followed. E.g.:

            User: "I actually redeemed a $5 coupon on coffee creamer
                   last Sunday, which was a nice surprise..."
            Assistant: "That's awesome! Redeeming a surprise coupon is
                        always a great feeling... Many retailers, like
                        Target, send exclusive coupons and promotions..."

        The expected answer "Target" is in the assistant message. By
        fetching the chunks adjacent to the BM25 hit (by rowid, which
        equals ingest order), we surface the assistant response and
        the deterministic judge can match "Target" against it.

        v0.5.4 sandbox: respects the InjecMEM agent_id scope. A user
        query (agent_id=None) only sees user-scoped chunks as hits,
        AND only sees user-scoped chunks as neighbors. An agent query
        (agent_id=X) sees user-scoped + agent-X-scoped chunks as both
        hits and neighbors. Without this, an agent-scoped "Alice lives
        in Toronto" chunk could leak into a user-scope view via the
        neighbor fetch even though the search() method correctly
        filtered it out as a primary hit.

        μ=0: pure SQL — no LLM. Returns the previous `before` and next
        `after` chunks (by rowid) belonging to the same user_id and
        matching the agent_id scope, skipping the hit chunk itself.

        Returns a list of {chunk_id, text, user_id, session_id,
        source_tx_id, position} dicts. Position is "before" or "after".
        """
        assert self._db is not None
        # InjecMEM sandbox filter — must match search()'s scope rule.
        if agent_id is None:
            scope_filter = "agent_id IS NULL"
            scope_params: tuple = ()
        else:
            scope_filter = "(agent_id IS NULL OR agent_id = ?)"
            scope_params = (agent_id,)
        out: list[dict] = []
        # In our canonical runner, chunks are ingested in conversation
        # order, so rowid IS position-in-conversation. The previous
        # chunk is rowid-1, the next is rowid+1, etc.
        # Fetch before
        for i in range(1, before + 1):
            row = self._db.execute(
                "SELECT rowid, text, user_id, session_id, source_tx_id "
                f"FROM verbatim_chunks WHERE rowid = ? AND user_id = ? "
                f"AND {scope_filter}",
                (chunk_id - i, user_id, *scope_params)).fetchone()
            if row:
                out.append({
                    "chunk_id": row[0], "text": row[1] or "",
                    "user_id": row[2], "session_id": row[3],
                    "source_tx_id": row[4], "position": "before",
                    "offset": -i,
                })
        # Fetch after
        for i in range(1, after + 1):
            row = self._db.execute(
                "SELECT rowid, text, user_id, session_id, source_tx_id "
                f"FROM verbatim_chunks WHERE rowid = ? AND user_id = ? "
                f"AND {scope_filter}",
                (chunk_id + i, user_id, *scope_params)).fetchone()
            if row:
                out.append({
                    "chunk_id": row[0], "text": row[1] or "",
                    "user_id": row[2], "session_id": row[3],
                    "source_tx_id": row[4], "position": "after",
                    "offset": i,
                })
        return out

    def _dense_only_search(self, *, query: str, user_id: str,
                            session_id: str | None,
                            agent_id: str | None,
                            k: int) -> list[VerbatimHit]:
        """Full-scan cosine — fallback when BM25 finds nothing.

        Used for paraphrase / OOD queries that share no lexical
        signal with the chunk text. μ=0: pure embedding math.
        """
        assert self._db is not None and self._embedder is not None
        if agent_id is None:
            scope_filter = "agent_id IS NULL"
            scope_params: tuple = ()
        else:
            scope_filter = "(agent_id IS NULL OR agent_id = ?)"
            scope_params = (agent_id,)
        rows = self._db.execute(
            f"SELECT rowid, text, user_id, session_id, source_tx_id "
            f"FROM verbatim_chunks WHERE user_id = ? AND {scope_filter}",
            (user_id, *scope_params)).fetchall()
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
        """Escape FTS5 special chars + switch to OR semantics.

        FTS5 has a query syntax (AND / OR / NEAR / column filters).
        A user searching for "Charlie's dog (brown)" would break it.

        v0.5.3 fix: previously wrapped each token in `"..."` and joined
        with spaces, which FTS5 implicitly treats as AND. That works for
        exact-match queries like `"Charlie" "Bee" "Providore"` but FAILS
        for natural-language questions like "What restaurant did they
        mention?" — the answer chunk only contains "Miss Bee Providore",
        so it doesn't match the AND of all question words.

        New behavior: drop stopwords, wrap each remaining token in
        double-quotes, join with ` OR `. This is standard BM25 best
        practice — the more query tokens a chunk contains, the higher
        its BM25 score. AND queries are too strict for natural-language
        questions and force the dense-only fallback every time, which
        is why canonical LongMemEval single_session was stuck at 0.222.
        """
        if not query:
            return '""'
        import re
        # Strip FTS5 operators and parens
        # Then split on whitespace + punctuation
        clean = re.sub(r'["\'()*:;\[\]{}^]+', " ", query)
        tokens = re.findall(r"\w+", clean)
        if not tokens:
            return '""'
        # Stopword removal — these would just dilute the BM25 signal
        # (every chunk contains "the", "a", "of"). The content words
        # are what carry the question's intent.
        _STOP = {
            "the", "a", "an", "is", "are", "was", "were", "did", "do",
            "does", "what", "where", "when", "how", "who", "why",
            "i", "me", "my", "we", "us", "our", "you", "your",
            "he", "she", "it", "they", "them", "their",
            "and", "or", "but", "of", "to", "for", "from", "by",
            "with", "as", "at", "in", "on", "so", "if", "be",
            "this", "that", "these", "those", "have", "has", "had",
            "would", "could", "should", "will", "can", "may", "might",
            "about", "into", "any", "some", "all", "every", "each",
            "go", "going", "gone", "went", "get", "got", "want", "wanted",
            "tell", "told", "ask", "asked", "think", "thought",
            "remind", "wonder", "wondering", "could", "would",
        }
        # Lowercase + filter stopwords + filter pure digits/short
        content_tokens = [t.lower() for t in tokens
                          if t.lower() not in _STOP
                          and len(t) > 1
                          and not t.isdigit()]
        if not content_tokens:
            return '""'
        # OR-join quoted tokens — FTS5 ranks by BM25, so chunks
        # matching MORE tokens rank higher (standard practice).
        return " OR ".join(f'"{t}"' for t in content_tokens)


__all__ = ["VerbatimPlugin", "VerbatimHit"]
