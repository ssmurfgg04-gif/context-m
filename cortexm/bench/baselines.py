"""Baseline retrievers for the neuro-symbolic delta table.

* ``bm25_rag``      — lexical RAG over raw chunks (the "context stuffer"
                      proxy: BEAM's Vanilla/RAG baselines score 12-25%).
* ``vector_only``   — our VSA palace WITHOUT the symbolic read path:
                      pure neural fact-level RAG. Isolates exactly what
                      the symbolic Trace contributes.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from cortexm.text.tokenizer import STOPWORDS, words


# ---------------------------------------------------------------- BM25
class BM25Index:
    def __init__(self, docs: list[dict], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs = docs
        self.doc_ids = [d["id"] for d in docs]
        self.doc_len = []
        self.tf: list[Counter] = []
        self.postings: dict[str, list[tuple[int, int]]] = {}
        for i, d in enumerate(docs):
            toks = [t for t in words(d["text"]) if t not in STOPWORDS]
            self.doc_len.append(len(toks))
            counts = Counter(toks)
            self.tf.append(counts)
            for t, c in counts.items():
                self.postings.setdefault(t, []).append((i, c))
        self.N = len(docs) or 1
        self.avgdl = (sum(self.doc_len) / self.N) if self.N else 1.0
        self.df = {t: len(p) for t, p in self.postings.items()}

    def search(self, query: str, k: int = 8) -> list[tuple[str, float]]:
        qtf = Counter(t for t in words(query) if t not in STOPWORDS)
        scores: dict[int, float] = {}
        for t, _ in qtf.items():
            postings = self.postings.get(t)
            if not postings:
                continue
            idf = math.log(1 + (self.N - self.df[t] + 0.5) / (self.df[t] + 0.5))
            for i, c in postings:
                dl = self.doc_len[i] or 1
                s = idf * (c * (self.k1 + 1)) / (
                    c + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
                scores[i] = scores.get(i, 0.0) + s
        top = sorted(scores.items(), key=lambda kv: -kv[1])[:k]
        return [(self.doc_ids[i], s) for i, s in top]

    def doc_text(self, doc_id: str) -> str:
        for d in self.docs:
            if d["id"] == doc_id:
                return d["text"]
        return ""


def bm25_context(index: BM25Index, query: str, k: int = 8) -> str:
    hits = index.search(query, k)
    parts = [index.doc_text(did)[:220] for did, _ in hits]
    return "\n".join(f"- {p}" for p in parts if p)


# ------------------------------------------------------- vector-only
def vector_only_context(memory, query: str, user_id: str, k: int = 8) -> str:
    """VSA palace search → source chunks. No temporal logic, no
    contradiction chains, no symbolic expansion — neural retrieval only."""
    q_vec = memory.palace.embedder.embed(query)
    scope = {f.id for f in memory.store.query_facts(user_id=user_id,
                                                    active=True)}
    hits = memory.palace.search(q_vec, max(k * 3, 24),
                                candidate_ids=scope or None)
    seen_chunks: list[str] = []
    seen_ids = set()
    for fid, score in hits:
        f = memory.store.get_fact(fid)
        if not f or not f.source_id:
            continue
        chunk = memory.store.get_chunk(f.source_id)
        if chunk and chunk["id"] not in seen_ids:
            seen_ids.add(chunk["id"])
            seen_chunks.append(chunk["text"][:240])
        if len(seen_chunks) >= k:
            break
    return "\n".join(f"- {c}" for c in seen_chunks)
