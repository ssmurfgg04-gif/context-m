"""Chaos mode — EAM-inspired zero-config auto-ingest.

arXiv insight (EAM / HeatherDB): the UX is "dump text in, intelligence
emerges." EAM conflates storage with inference — we reject that as
a black box. But the UX itself is the right default: most users
shouldn't have to tune patterns, configure idiolect dictionaries,
or wire bridges. They should just dump text in.

This module provides a one-call auto-ingest path:

    chaos_ingest(mem, list_of_texts, user_id="default")

That runs the FULL pipeline with sensible defaults:
    - per-user idiolect normalizer (built-in text-speak escape hatch)
    - DisSim recursive simplification (splits compound sentences so
      the pattern extractor sees one fact per clause)
    - pattern extractor (μ=0, deterministic)
    - lifecycle / contradiction / palace encoding / edges

The user can LATER opt into deterministic mode (call mem.add()
directly) when they want explicit control over the pipeline. This
matches EAM's "magic" UX without inheriting EAM's opacity — every
fact has full provenance, the Memory Git ancestry is preserved,
and the audit log records which sub-module extracted what.

Why the name: "chaos mode" because raw chaotic text → structured
facts, no user configuration required. Let EAM sell the magic;
Context-M keeps the receipts.
"""
from __future__ import annotations

from typing import Iterable

from cortexm.config import Config
from cortexm.text.dissim import DisSimSplitter
from cortexm.text.embedder import HashingEmbedder
from cortexm.text.idiolect import PerUserIdiolectNormalizer


# Module-level singletons per Memory instance — idiolect + dissim are
# stateful (idiolect accumulates per-user slang observations; dissim
# is stateless but expensive to construct because it compiles regex).
# We cache one of each per Memory so the host doesn't pay construction
# cost on every chaos_ingest call.
_CACHE_ATTR = "_chaos_cache"


def _get_cache(mem):
    """Lazy-init the chaos-mode cache on a Memory instance."""
    if not hasattr(mem, _CACHE_ATTR):
        embedder = HashingEmbedder(mem.palace.dims, mem.palace.cfg.seed)
        setattr(mem, _CACHE_ATTR, {
            "idiolect": PerUserIdiolectNormalizer(embedder),
            "dissim": DisSimSplitter(max_depth=2),
            "embedder": embedder,
        })
    return getattr(mem, _CACHE_ATTR)


def chaos_ingest(mem, texts, *, user_id: str = "default",
                 agent_id: str | None = None,
                 run_id: str | None = None) -> dict:
    """Auto-ingest raw text via the full unmess + dissim + writer pipeline.

    For each text:
        1. observe idiolect (per-user slang dictionary accumulates)
        2. normalize via idiolect (text-speak + kNN slang → canonical)
        3. split compound sentences via DisSim (recursive syntactic)
        4. for each clause: writer.add() — chunk insert + pattern
           extract + quarantine + lifecycle + palace encoding + edges

    Returns a stats dict matching Memory.add()'s shape (so callers
    can drop chaos_ingest in place of mem.add() without changes).
    """
    cache = _get_cache(mem)
    idiolect = cache["idiolect"]
    dissim = cache["dissim"]

    if isinstance(texts, str):
        texts = [texts]

    total_inserted = 0
    all_results: list = []
    total_tokens = 0
    for text in texts:
        if not text or not text.strip():
            continue
        # 1. observe idiolect
        idiolect.observe(user_id, text)
        # 2. normalize
        norm = idiolect.normalize(user_id, text)
        # 3. split into clauses
        clauses = [c.text for c in (dissim.simplify_text(norm) or [norm])]
        # 4. ingest each clause through the standard writer pipeline
        for clause in clauses:
            if not clause or not clause.strip():
                continue
            out = mem.add(
                [{"role": "user", "content": clause}],
                user_id=user_id, agent_id=agent_id, run_id=run_id)
            total_inserted += out.get("stats", {}).get("facts_inserted", 0)
            total_tokens += out.get("stats", {}).get("tokens", 0)
            all_results.extend(out.get("results", []))

    return {
        "event": "CHAOS_INGEST",
        "results": all_results,
        "commit": None,  # multiple commits per call
        "stats": {
            "messages": len(texts),
            "tokens": total_tokens,
            "facts_inserted": total_inserted,
            "llm_calls": 0,
        },
    }


__all__ = ["chaos_ingest"]
