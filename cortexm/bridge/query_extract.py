"""Query-time extraction — store raw + extract when needed.

arxiv research (Con #3 from user's research): the agent memory literature
shows that forcing extraction at ingest time (μ=0) is the wrong target.
Better: store raw text chunks + embeddings at ingest; extract triples
lazily at query time when the query disambiguates context.

This module implements the hybrid via MemoryWriter as the single write
path. Previous versions had a bespoke `_store_fact` that wrote to the
Trace without quarantine / contradiction / lifecycle / palace encoding /
edge wiring — that bypassed MemoryWriter's pronoun resolution and
entity tracking, which is why the query-time path measured *worse* than
baseline (0.835 vs 1.017 recall). Now both ingest and query-time
extraction go through `MemoryWriter.add()` / `ingest_candidates()` so
the standard pipeline runs end-to-end.

The bi-temporal model is preserved: every fact has `valid_at` (when
the fact became true) AND `extracted_at` (when we materialized it).
The `extracted_at` timestamp is stored in the fact's `provenance` dict
under the `extracted_at` key, so the existing schema doesn't need a
migration. Both axes are queryable via `provenance ->> 'extracted_at'`.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any

from cortexm.util import iso, new_id


@dataclass
class RawChunk:
    """A raw text chunk stored at ingest, awaiting query-time extraction.

    This is now a logical handle only — the chunk itself lives in the
    standard `chunks` table managed by TraceStore.add_chunk(). This
    dataclass is kept for backward compatibility with callers that
    inspect the return value of `QueryTimeExtractor.ingest()`.
    """
    chunk_id: str
    text: str
    user_id: str
    valid_at: str
    extracted_at: str | None = None
    source_hash: str = ""
    embedding: Any | None = None
    consumed: bool = False


class QueryTimeExtractor:
    """Hybrid ingest + query-time extraction orchestrator.

    NOW routes every write through `MemoryWriter` so query-time-extracted
    facts go through the SAME quarantine, contradiction, lifecycle, palace
    encoding, and edge wiring as ingest-time facts. No bespoke DB writes,
    no separate `_store_fact` that bypasses the standard pipeline.

    Usage (preferred — let the orchestrator build the writer):
        extractor = QueryTimeExtractor(
            palace, store, embedder,
            writer=mem,  # MemoryWriter — REQUIRED
            dissim=dissim, idiolect=idiolect,
            pattern_extractor=mem.extractor)

    Ingest (delegates to MemoryWriter.add — does chunk + pattern + palace
    + lifecycle + edges, all μ=0):
        out = extractor.ingest(text, user_id, valid_at="2026-03-01")

    Query (palace search → for each un-extracted chunk, lazily extract
    via dissim + pattern extractor + writer.ingest_candidates):
        results = extractor.query("where does Alice work?", user_id)
    """

    def __init__(self, palace, store, embedder, *, writer,
                 reader=None, dissim=None, idiolect=None,
                 pattern_extractor=None, cleanup=None, overlay=None) -> None:
        # writer is now REQUIRED — every write path goes through it
        if writer is None:
            raise ValueError(
                "QueryTimeExtractor now requires a `writer` (MemoryWriter) "
                "argument so query-time-extracted facts route through the "
                "standard pipeline. Previous versions bypassed MemoryWriter "
                "and measured 0.835 vs 1.017 recall — this regression is "
                "the reason for the refactor.")
        self.palace = palace
        self.store = store
        self.embedder = embedder
        self.writer = writer
        # reader is OPTIONAL but recommended — when present, query()
        # delegates primary retrieval to it so query-time path has
        # parity with the ingest path's mem.search() (intent planner,
        # fusion, entity-hop, mentioned-damping). Without a reader,
        # query() falls back to raw palace.search() which is dumber
        # and was the cause of the 0.835 recall regression.
        self.reader = reader
        self.dissim = dissim
        self.idiolect = idiolect
        # default to the writer's own extractor so the SAME patterns run
        # at query time as at ingest time
        self.pattern_extractor = pattern_extractor or writer.extractor
        self.cleanup = cleanup
        self.overlay = overlay

    # ------------------------------------------------------------------
    def ingest(self, text: str, user_id: str = "default",
              valid_at: str | None = None,
              run_pattern: bool = True) -> dict:
        """Store raw text chunk + embedding. Run pattern extractor as
        best-effort (μ=0 path) by delegating to MemoryWriter.add().

        - run_pattern=True  (default): full MemoryWriter.add() — chunk
          insert + pattern extraction + lifecycle + palace encoding +
          edges + Datalog materialization. Returns the writer's result
          dict.
        - run_pattern=False: raw-only — just `store.add_chunk()` + palace
          embed, no extraction. The chunk is queryable; facts will be
          extracted lazily at query time. Returns a small handle dict.

        CRITICAL: chunks are added to the palace so query-time search
        can retrieve them by VSA similarity. This is done in BOTH paths
        (writer.add() does it implicitly via encode_fact; raw-only path
        does it explicitly here).
        """
        # observe idiolect (mutates normalizer state for later queries)
        if self.idiolect:
            self.idiolect.observe(user_id, text)

        if not run_pattern:
            # raw-only: skip extraction, just store the chunk + embed it
            ts = self._parse_ts(valid_at)
            chunk_id = self.store.add_chunk(
                text, user_id=user_id, ts=ts,
                source="query_time_raw")
            emb = self.embedder.embed(text)
            try:
                self.palace.add(chunk_id, emb)
            except Exception:
                pass
            return {"event": "RAW_CHUNK", "chunk_id": chunk_id,
                    "results": [], "commit": None,
                    "stats": {"messages": 1,
                              "tokens": len(text) // 4,
                              "facts_inserted": 0, "llm_calls": 0}}

        # full path: delegate to MemoryWriter.add() — this adds the
        # chunk via store.add_chunk and encodes any extracted FACTS
        # into the palace (via encode_fact on the bound triple vector).
        # We ADDITIONALLY embed the raw chunk text into the palace so
        # query-time palace.search() can retrieve the chunk itself by
        # text similarity (fact vectors are bound role-fillers, not
        # text embeddings, so they don't help find the source chunk).
        # CRITICAL: normalize text via idiolect BEFORE writer.add() so
        # the pattern extractor sees the same canonical text as the
        # +unmess path — otherwise patterns miss on slang that the
        # normalizer could fix (e.g. "I work @ Microsoft" → "I work
        # at Microsoft" via the text-speak escape hatch).
        ts = self._parse_ts(valid_at)
        norm_text = text
        if self.idiolect:
            norm_text = self.idiolect.normalize(user_id, text)
        out = self.writer.add(
            [{"role": "user", "content": norm_text}],
            user_id=user_id, ts=ts, source="query_time_ingest")
        # pull the most-recent chunk for this user — that's the chunk
        # writer.add just inserted — and embed it into the palace
        try:
            row = self.store.conn.execute(
                "SELECT id FROM chunks WHERE user_id=? "
                "ORDER BY ts DESC LIMIT 1", (user_id,)).fetchone()
            if row:
                emb = self.embedder.embed(norm_text)
                self.palace.add(row[0], emb)
        except Exception:
            pass
        return out

    # ------------------------------------------------------------------
    def query(self, query_text: str, user_id: str = "default",
              k: int = 5, extract_fresh: bool = True
             ) -> list[dict]:
        """Retrieve facts relevant to query; lazily extract from
        un-extracted chunks through MemoryWriter.ingest_candidates
        so the standard pipeline runs end-to-end.

        Two-pass retrieval:
          PASS 1 — Delegate to the structured reader (mem.search) when
            available. This gives the query-time path retrieval PARITY
            with the ingest path — same intent planner, fusion,
            entity-hop, mentioned-damping. On clean queries like
            "What is the works_at of user0?" the reader returns the
            fact directly via the Trace, no raw chunk search needed.

          PASS 2 — If the reader missed (or no reader was supplied),
            fall back to raw palace.search() over chunk text embeddings
            and lazily extract from un-extracted chunks via dissim +
            pattern extractor + writer.ingest_candidates. This is the
            "query-time extraction" win — patterns that missed at ingest
            on slangy text now match after idiolect normalization +
            dissim simplification, and the resulting facts land in the
            Trace through the same quarantine / lifecycle / palace /
            edges pipeline as ingest-time facts.

        Returns list of dicts (shape compatible with both the reader
        result format and the raw-chunk fallback format):
          {"fact": {...}|None, "retrieval_path": "...",
           "source_chunk_id": "...", "extracted_at": "..."|None,
           "memory": "...", "score": float,
           "raw_text": "..." (only when fact is None)}
        """
        # NOTE: we deliberately do NOT normalize the query at the top
        # level — the reader's intent planner expects clean structured
        # queries like "What is the works_at of user0?" and idiolect
        # normalization (with the text-speak escape hatch) can rewrite
        # "is" / "of" / short tokens in ways that confuse the planner.
        # Normalization is only applied to the raw-chunk retrieval path
        # (PASS 2) where it actually helps match chunk text.
        q_text = query_text
        now = iso(_dt.datetime.now(_dt.timezone.utc))
        out: list[dict] = []

        # --- PASS 1: reader (structured retrieval) -----------------------
        if self.reader is not None:
            try:
                # reader.search() returns a RetrievalResult dataclass;
                # .memories() converts to list of {id, memory, score, ...}
                # dicts — same shape as Memory.search()'s `results`.
                # We parse the "subject | relation | value" memory string
                # back into structured fields so downstream checks (e.g.
                # the BEAM benchmark's value-match) work the same as
                # for query-time-extracted facts.
                rr = self.reader.search(
                    q_text, user_id=user_id, k=k)
                for r in rr.memories():
                    mem_str = r.get("memory", "")
                    parts = [p.strip() for p in mem_str.split("|")]
                    fact_dict = {**r,
                                 "subject": parts[0] if len(parts) > 0 else "",
                                 "relation": parts[1] if len(parts) > 1 else "",
                                 "value": parts[2] if len(parts) > 2 else ""}
                    out.append({
                        "fact": fact_dict,
                        "retrieval_path": "reader",
                        "source_chunk_id": r.get("id", ""),
                        "extracted_at": None,  # reader facts lack this
                        "memory": mem_str,
                        "score": r.get("score", 0.0),
                    })
            except Exception:
                pass

        # --- PASS 2: raw chunk retrieval + lazy reextract ---------------
        # Only run if the reader returned < k results, OR no reader.
        if len(out) < k:
            q_emb = self.embedder.embed(q_text)
            results = self.palace.search(q_emb, k=k * 2)
            if results:
                chunks: list[dict] = []
                seen_chunk_ids = {r.get("source_chunk_id", "")
                                  for r in out if r.get("source_chunk_id")}
                for chunk_id, score in results:
                    if chunk_id in seen_chunk_ids:
                        continue
                    chunk = (self.store.get_chunk(chunk_id)
                             if hasattr(self.store, "get_chunk") else None)
                    if chunk:
                        chunk["_score"] = float(score)
                        chunks.append(chunk)
                for chunk in chunks[: k - len(out)]:
                    already_extracted = self._chunk_has_facts(chunk["id"])
                    if extract_fresh and not already_extracted:
                        self._reextract_chunk(chunk, user_id, now)
                    facts = self._facts_for_chunk(chunk["id"])
                    if facts:
                        for f in facts:
                            out.append({
                                "fact": f,
                                "retrieval_path": "query_time_pattern",
                                "source_chunk_id": chunk["id"],
                                "extracted_at": (f.get("provenance") or {})
                                                .get("extracted_at") or now,
                                "memory": f"{f.get('subject','')} | "
                                          f"{f.get('relation','')} | "
                                          f"{f.get('value','')}",
                                "score": chunk.get("_score", 0.0),
                            })
                    else:
                        out.append({
                            "fact": None,
                            "retrieval_path": "raw_chunk",
                            "source_chunk_id": chunk["id"],
                            "extracted_at": None,
                            "raw_text": chunk["text"],
                            "memory": chunk["text"],
                            "score": chunk.get("_score", 0.0),
                        })
        return out

    # ------------------------------------------------------------------
    # Internals — all writes go through MemoryWriter.ingest_candidates
    # ------------------------------------------------------------------
    def _reextract_chunk(self, chunk: dict, user_id: str, now_iso: str
                         ) -> int:
        """Run dissim + pattern extractor on chunk text; commit through
        writer.ingest_candidates so the standard pipeline runs.

        Returns the number of candidates sent to the writer (NOT the
        number of facts inserted — the writer's lifecycle may merge,
        skip, or supersede duplicates, which is exactly the parity we
        want with the ingest path).
        """
        text = chunk["text"]
        # normalize via idiolect if available (helps pronoun resolution
        # on text that was stored raw without idiolect normalization)
        if self.idiolect:
            text = self.idiolect.normalize(user_id, text)
        # split into clauses via dissim (recursive syntactic splitting)
        clauses = [text]
        if self.dissim:
            simplified = self.dissim.simplify_text(text)
            if simplified:
                clauses = [c.text for c in simplified]
        # extract candidates from each clause
        from cortexm.bridge.patterns import ExtractionContext
        from datetime import datetime, timezone
        all_candidates: list = []
        for clause in clauses:
            ctx = ExtractionContext(
                user_id=user_id,
                ts=datetime.now(timezone.utc),
                # the writer's name-of / lexicon helpers are private
                # by convention only — call them to give the pattern
                # extractor the same pronoun / entity hints that the
                # ingest path gets
                subject_name=self._safe_name_of(user_id),
                lexicon=self._safe_lexicon(user_id))
            try:
                cs = self.pattern_extractor.extract(clause, ctx)
                all_candidates.extend(cs)
            except Exception:
                continue
        if not all_candidates:
            return 0
        # commit through the standard pipeline — `source` ends up in
        # the provenance dict as `enriched_by`, so audits can always
        # tell these facts came from the query-time path
        try:
            inserted = self.writer.ingest_candidates(
                all_candidates, user_id=user_id,
                chunk_id=chunk["id"],
                ts=datetime.now(timezone.utc),
                source="query_time_reextract")
            return inserted
        except Exception:
            return 0

    # ------------------------------------------------------------------
    def _chunk_has_facts(self, chunk_id: str) -> bool:
        """A chunk counts as 'extracted' if any fact in the trace has
        an EXTRACTED_FROM edge pointing at it. No schema migration
        needed — uses the existing edges table."""
        try:
            row = self.store.conn.execute(
                "SELECT 1 FROM edges WHERE dst=? AND kind='EXTRACTED_FROM' "
                "LIMIT 1", (chunk_id,)).fetchone()
            return row is not None
        except Exception:
            return False

    def _facts_for_chunk(self, chunk_id: str) -> list[dict]:
        """Active facts whose source_id is this chunk."""
        try:
            rows = self.store.conn.execute(
                "SELECT * FROM facts WHERE source_id=? AND is_active=1 "
                "ORDER BY confidence DESC", (chunk_id,)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                # provenance is stored as JSON text — parse to dict so
                # callers can do prov.get(...) without surprise
                if isinstance(d.get("provenance"), str):
                    try:
                        import json
                        d["provenance"] = json.loads(
                            d["provenance"] or "{}")
                    except Exception:
                        d["provenance"] = {}
                out.append(d)
            return out
        except Exception:
            return []

    # ------------------------------------------------------------------
    def _safe_name_of(self, user_id: str) -> str | None:
        """Pull the user's display name from the writer if available."""
        try:
            return self.writer._name_of(user_id)
        except Exception:
            return None

    def _safe_lexicon(self, user_id: str) -> set:
        """Pull the user's learned lexicon from the writer if available."""
        try:
            return self.writer._lexicon(user_id)
        except Exception:
            return set()

    @staticmethod
    def _parse_ts(valid_at: str | None):
        if not valid_at:
            return None
        try:
            from cortexm.trace.store import parse_ts
            return parse_ts(valid_at)
        except Exception:
            return None


__all__ = ["QueryTimeExtractor", "RawChunk"]
