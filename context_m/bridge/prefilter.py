"""Query-aware triple pre-filter (HippoRAG 2 lineage).

arXiv:2505.14832 (HippoRAG 2) reports a 7% F1 gain from query-aware
passage/triple filtering BEFORE the symbolic-vs-neural fusion step.
The insight: when you bind candidate triples into a holographic
superposition for VSA retrieval, every IRRELEVANT triple injects noise
into the superposition. Pre-filtering with a cheap lexical+semantic
scorer drops the noise and lifts precision at the fusion output.

Context-M's bridge/reader.py currently:
  1. VSA probe → top-K candidate facts by cosine sim
  2. symbolic dereference → full fact rows + edges
  3. PPR over the local fact graph → multi-hop boost
  4. fusion (VSA + PPR + symbolic) → final rank

This module inserts between steps 1 and 3:
  1.5  QUERY-AWARE TRIPLE PRE-FILTER
       For each candidate fact, compute:
         * lexical_score  — Jaccard of content words in (query, fact text)
         * semantic_score — cosine(query_emb, fact_text_emb)
         * relation_match — +0.2 if the query's RELATION_HINTS include
                             the fact's relation (e.g. "where does X live"
                             → relation_hint=lives_in → match)
       Combined weighted score; drop facts below threshold.

This is μ=0: deterministic scorer, no LLM. The cost is O(K) cosine sims
on the top-K candidates (K=20-50 typically), ~50μs total per query.

The 7% F1 gain HippoRAG 2 reports is for the LLM-triple-filter variant;
our deterministic variant should capture most of the lift on natural-
language queries where lexical+relation overlap is a strong signal.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from context_m.text.tokenizer import STOPWORDS, words


@dataclass
class PrefilterStats:
    n_in: int
    n_kept: int
    n_dropped: int
    min_score: float
    max_score: float
    mean_score: float


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _content_word_set(text: str) -> set[str]:
    return {w.lower() for w in words(text) if w.lower() not in STOPWORDS}


def prefilter_triples(
        candidates: list,
        query: str,
        *,
        query_emb: np.ndarray | None = None,
        fact_text_fn=None,
        relation_hints: list[str] | None = None,
        embedder=None,
        threshold: float = 0.08,
        weights: tuple[float, float, float] = (0.45, 0.45, 0.10),
        min_keep: int = 3,
) -> tuple[list, PrefilterStats]:
    """Filter candidate facts by query relevance BEFORE fusion.

    Parameters
    ----------
    candidates : list of Fact-like objects (must have .subject, .relation,
                 .value, optionally .id and .memory/.note)
    query : the user's raw query string
    query_emb : pre-computed query embedding (np.ndarray). If None and an
                embedder is provided, we compute it here.
    fact_text_fn : callable(fact) -> str, the natural-language rendering
                   of the fact to score against. Defaults to
                   f"{subject} {relation} {value}".
    relation_hints : list of relation names the query seems to be asking
                     about (from RELATION_HINTS in reader.py). Each
                     candidate whose .relation is in this list gets a
                     +0.2 boost.
    embedder : optional object with .embed(text) -> np.ndarray, used to
                compute fact_text embeddings for semantic scoring.
    threshold : combined score below this → drop. Conservative default
                (0.08) keeps most candidates; tune up for higher precision.
    weights : (lexical, semantic, relation_match) weights summing to ~1.
    min_keep : always keep at least this many candidates (top-K by score),
               even if all are below threshold — guarantees fusion has
               something to rank.

    Returns (filtered_list, stats).
    """
    if not candidates:
        return [], PrefilterStats(0, 0, 0, 0.0, 0.0, 0.0)

    q_words = _content_word_set(query)
    if query_emb is None and embedder is not None:
        try:
            query_emb = embedder.embed(query)
        except Exception:
            query_emb = None

    rel_set = set(relation_hints) if relation_hints else set()
    w_lex, w_sem, w_rel = weights
    out: list[tuple[float, int]] = []   # (score, original_idx)
    n = len(candidates)
    scores = [0.0] * n

    # pre-compute fact embeddings if we have an embedder (batched)
    fact_embs: list[np.ndarray | None] = [None] * n
    if embedder is not None and w_sem > 0:
        for i, c in enumerate(candidates):
            try:
                txt = (fact_text_fn(c) if fact_text_fn
                        else _default_fact_text(c))
                fact_embs[i] = embedder.embed(txt)
            except Exception:
                fact_embs[i] = None

    for i, c in enumerate(candidates):
        txt = (fact_text_fn(c) if fact_text_fn else _default_fact_text(c))
        c_words = _content_word_set(txt)
        lex = _jaccard(q_words, c_words)
        sem = 0.0
        if query_emb is not None and fact_embs[i] is not None:
            try:
                sem = float(np.dot(query_emb, fact_embs[i]))
                # cosine sim in [-1, 1] → normalize to [0, 1]
                sem = max(0.0, (sem + 1.0) / 2.0)
            except Exception:
                sem = 0.0
        rel = 1.0 if (rel_set and getattr(c, "relation", "") in rel_set) else 0.0
        score = w_lex * lex + w_sem * sem + w_rel * rel
        scores[i] = score
        out.append((score, i))

    # always keep at least min_keep — sort desc, take top min_keep
    out.sort(key=lambda x: -x[0])
    kept_idx = [i for s, i in out if s >= threshold]
    if len(kept_idx) < min_keep:
        for s, i in out:
            if i not in kept_idx:
                kept_idx.append(i)
                if len(kept_idx) >= min_keep:
                    break
    kept_set = set(kept_idx)
    filtered = [candidates[i] for i in range(n) if i in kept_set]
    kept_scores = [scores[i] for i in range(n) if i in kept_set]
    stats = PrefilterStats(
        n_in=n,
        n_kept=len(filtered),
        n_dropped=n - len(filtered),
        min_score=min(kept_scores) if kept_scores else 0.0,
        max_score=max(kept_scores) if kept_scores else 0.0,
        mean_score=(sum(kept_scores) / len(kept_scores)) if kept_scores else 0.0,
    )
    return filtered, stats


def _default_fact_text(f) -> str:
    """Natural-language rendering of a fact for scoring.

    The pattern library produces facts with .subject, .relation, .value.
    Reader.RetrievalResult wraps them with .memory (a NL string). We
    prefer .memory if available, else fall back to the triple.
    """
    mem = getattr(f, "memory", None)
    if mem:
        return mem
    subj = getattr(f, "subject", "") or ""
    rel = getattr(f, "relation", "") or ""
    val = getattr(f, "value", "") or ""
    # turn "lives_in" into "lives in" for slightly better lexical overlap
    rel_nl = rel.replace("_", " ")
    return f"{subj} {rel_nl} {val}".strip()


__all__ = ["prefilter_triples", "PrefilterStats"]
