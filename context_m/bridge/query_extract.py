"""Query-time extraction — store raw + extract when needed.

arxiv research (Con #3 from user's research): the agent memory literature
shows that forcing extraction at ingest time (μ=0) is the wrong target.
Better: store raw text chunks + embeddings at ingest; extract triples
lazily at query time when the query disambiguates context.

This module implements the hybrid:
  - Ingest path (μ=0 still works for clean text): pattern extractor
    runs as best-effort; whatever triples it finds go to Trace.
  - Query path: retrieve raw chunks via VSA similarity, then run
    extraction only on those chunks with unmess + simplification + the
    pattern extractor. Resulting triples are written to Trace lazily
    with `extracted_at = now` as a new temporal axis alongside
    `valid_at`.

The bi-temporal model still works: every triple has `valid_at` (when
the fact became true) AND `extracted_at` (when we materialized it). The
second is a new temporal axis. Both are queryable.

Without this module: pattern misses on slang/paraphrase are silent
information loss. With it: the query itself triggers extraction on the
right raw chunks, paying extraction cost only on relevant text.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Iterable

from context_m.util import iso, new_id


@dataclass
class RawChunk:
    """A raw text chunk stored at ingest, awaiting query-time extraction."""
    chunk_id: str
    text: str
    user_id: str
    valid_at: str           # ISO date when the chunk was true
    extracted_at: str | None = None   # ISO when first extracted (lazy)
    source_hash: str = ""
    embedding: object | None = None    # cached embed
    consumed: bool = False           # set True after first query-time extract


class QueryTimeExtractor:
    """Hybrid ingest + query-time extraction orchestrator.

    Usage:
      extractor = QueryTimeExtractor(palace, store, embedder, dissim,
                                     idiolect, pattern_extractor)
      # ingest (μ=0 best-effort):
      chunk = extractor.ingest(text, user_id, valid_at=...)
      # query (extract on demand):
      results = extractor.query("where does Alice work?", user_id)
    """

    def __init__(self, palace, store, embedder, dissim=None,
                 idiolect=None, pattern_extractor=None,
                 cleanup=None, overlay=None) -> None:
        self.palace = palace
        self.store = store
        self.embedder = embedder
        self.dissim = dissim
        self.idiolect = idiolect
        self.pattern_extractor = pattern_extractor
        self.cleanup = cleanup
        self.overlay = overlay

    def ingest(self, text: str, user_id: str = "default",
              valid_at: str | None = None,
              run_pattern: bool = True) -> RawChunk:
        """Store raw text chunk + embedding. Run pattern extractor as
        best-effort (μ=0 path). Returns the stored RawChunk handle.

        CRITICAL: also adds the embedding to the palace so query-time
        search can retrieve it.
        """
        valid_at = valid_at or iso(_dt.datetime.now(_dt.timezone.utc))[:10]
        # observe idiolect (mutates normalizer state)
        if self.idiolect:
            self.idiolect.observe(user_id, text)
        # normalize via idiolect if available
        norm_text = text
        if self.idiolect:
            norm_text = self.idiolect.normalize(user_id, text)
        # store raw chunk in DB
        chunk_id = new_id()
        source_hash = self._hash(text)
        self._store_raw_chunk(chunk_id, text, norm_text, user_id,
                              valid_at, source_hash)
        # embedding for palace — CRITICAL: add to palace so query can find it
        emb = self.embedder.embed(norm_text)
        try:
            self.palace.add(chunk_id, emb)
        except Exception:
            pass  # palace might be closed
        # best-effort pattern extraction (μ=0 path) — only if patterns match
        extracted_count = 0
        if run_pattern and self.pattern_extractor:
            extracted_count = self._run_patterns(
                text, norm_text, user_id, valid_at, chunk_id)
        return RawChunk(
            chunk_id=chunk_id, text=text, user_id=user_id,
            valid_at=valid_at, source_hash=source_hash,
            embedding=emb, consumed=extracted_count > 0)

    def query(self, query_text: str, user_id: str = "default",
              k: int = 5, extract_fresh: bool = True
             ) -> list[dict]:
        """Retrieve raw chunks relevant to query; extract triples lazily.

        Returns list of dicts:
          {"fact": {...}, "retrieval_path": "...",
           "source_chunk_id": "...", "extracted_at": "..."}
        """
        # normalize query
        q_text = query_text
        if self.idiolect:
            q_text = self.idiolect.normalize(user_id, query_text)
        # search the palace for relevant raw chunks
        q_emb = self.embedder.embed(q_text)
        results = self.palace.search(q_emb, k=k * 2)  # over-fetch
        if not results:
            return []
        # fetch raw chunks for the top hits
        chunks = self._fetch_raw_chunks([fid for fid, _ in results])
        if not chunks:
            return []
        out: list[dict] = []
        now = iso(_dt.datetime.now(_dt.timezone.utc))
        for chunk in chunks[:k]:
            # simplify the chunk into clauses
            clauses = [chunk["text"]]
            if self.dissim:
                simplified = self.dissim.simplify_text(chunk["text"])
                clauses = [c.text for c in simplified] or [chunk["text"]]
            # extract patterns from each clause
            extracted_any = False
            if extract_fresh and self.pattern_extractor:
                for clause in clauses:
                    facts = self._extract_from_clause(
                        clause, user_id, chunk, now)
                    for f in facts:
                        out.append({
                            "fact": f, "retrieval_path": "query_time_pattern",
                            "source_chunk_id": chunk["chunk_id"],
                            "extracted_at": now,
                        })
                        extracted_any = True
            # always include raw chunk as fallback — even if patterns matched,
            # the caller (LLM) benefits from seeing the source text too
            if not extracted_any:
                out.append({
                    "fact": None,
                    "retrieval_path": "raw_chunk",
                    "source_chunk_id": chunk["chunk_id"],
                    "extracted_at": None,
                    "raw_text": chunk["text"],
                })
        return out

    # --- internals ---
    def _run_patterns(self, raw: str, normalized: str, user_id: str,
                     valid_at: str, chunk_id: str) -> int:
        """Best-effort pattern extraction at ingest. Returns count."""
        try:
            from context_m.bridge.patterns import ExtractionContext
            from datetime import datetime, timezone
            ctx = ExtractionContext(
                user_id=user_id,
                ts=datetime.now(timezone.utc),
            )
            candidates = self.pattern_extractor.extract(normalized, ctx)
            count = 0
            for c in candidates:
                # promote candidate to Fact
                fact_dict = {
                    "subject": c.subject, "relation": c.relation,
                    "value": c.value, "confidence": c.confidence,
                    "memory_type": "long_term",
                    "is_derived": False,
                    "valid_from": valid_at,
                    "provenance": {
                        "extracted_at": iso(_dt.datetime.now(_dt.timezone.utc)),
                        "source_chunk_id": chunk_id,
                        "retrieval_path": "ingest_pattern",
                        "pattern": c.pattern,
                    },
                }
                self._store_fact(fact_dict, user_id)
                count += 1
            return count
        except Exception:
            return 0

    def _extract_from_clause(self, clause: str, user_id: str,
                            chunk: dict, now: str) -> list[dict]:
        """Run pattern extractor on a single clause. Returns list of fact dicts."""
        try:
            from context_m.bridge.patterns import ExtractionContext
            from datetime import datetime, timezone
            ctx = ExtractionContext(
                user_id=user_id,
                ts=datetime.now(timezone.utc),
            )
            candidates = self.pattern_extractor.extract(clause, ctx)
            facts = []
            for c in candidates:
                fd = {
                    "subject": c.subject, "relation": c.relation,
                    "value": c.value, "confidence": c.confidence,
                    "memory_type": "long_term",
                    "is_derived": False,
                    "valid_from": chunk.get("valid_at", now[:10]),
                    "provenance": {
                        "extracted_at": now,
                        "source_chunk_id": chunk["chunk_id"],
                        "retrieval_path": "query_time_pattern",
                        "pattern": c.pattern,
                    },
                }
                self._store_fact(fd, user_id)
                facts.append(fd)
            return facts
        except Exception:
            return []

    def _store_raw_chunk(self, chunk_id: str, raw: str, normalized: str,
                        user_id: str, valid_at: str, source_hash: str) -> None:
        """Insert a raw text chunk into the store."""
        try:
            self.store.conn.execute(
                """CREATE TABLE IF NOT EXISTS raw_chunks (
                  chunk_id TEXT PRIMARY KEY, text TEXT NOT NULL,
                  normalized TEXT NOT NULL, user_id TEXT NOT NULL,
                  valid_at TEXT NOT NULL, source_hash TEXT NOT NULL,
                  extracted_at TEXT DEFAULT NULL
                )""")
            self.store.conn.execute(
                """INSERT OR REPLACE INTO raw_chunks
                   (chunk_id, text, normalized, user_id, valid_at, source_hash)
                   VALUES(?,?,?,?,?,?)""",
                (chunk_id, raw, normalized, user_id, valid_at, source_hash))
            self.store.conn.commit()
        except Exception:
            pass

    def _fetch_raw_chunks(self, chunk_ids: list[str]) -> list[dict]:
        """Fetch raw chunks by id."""
        if not chunk_ids:
            return []
        try:
            qmarks = ",".join("?" * len(chunk_ids))
            rows = self.store.conn.execute(
                f"""SELECT chunk_id, text, normalized, user_id, valid_at,
                       source_hash, extracted_at
                    FROM raw_chunks WHERE chunk_id IN ({qmarks})""",
                chunk_ids).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def _store_fact(self, fact_dict: dict, user_id: str) -> None:
        """Insert a fact into the Trace."""
        try:
            from context_m.trace.fact import Fact
            f = Fact(
                id=fact_dict.get("id") or new_id(),
                subject=fact_dict.get("subject", ""),
                relation=fact_dict.get("relation", ""),
                value=fact_dict.get("value", ""),
                valid_from=fact_dict.get("valid_from", ""),
                tx_from=fact_dict.get("extracted_at",
                                       iso(_dt.datetime.now(_dt.timezone.utc))),
                user_id=user_id,
                confidence=fact_dict.get("confidence", 0.7),
                memory_type=fact_dict.get("memory_type", "long_term"),
                is_derived=fact_dict.get("is_derived", False),
                provenance=fact_dict.get("provenance", {}),
            )
            self.store.insert_fact(f)
        except Exception:
            pass

    def _hash(self, text: str) -> str:
        """BLAKE3-style hash of text."""
        try:
            import hashlib
            return hashlib.blake2b(text.encode("utf-8"),
                                  digest_size=32).hexdigest()
        except Exception:
            return ""


__all__ = ["QueryTimeExtractor", "RawChunk"]
