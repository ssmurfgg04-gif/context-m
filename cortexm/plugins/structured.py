"""Structured Tier plugin — wraps the existing Memory class.

This is NOT a re-implementation. The structured tier (bi-temporal
Trace + VSA Palace + Bridge reader/writer) already exists and has
400+ tests. This plugin is just an ADAPTER so the kernel can mount
it alongside the verbatim plugin.

The plugin takes a "memory" service (a fully-constructed
``cortexm.api.memory.Memory`` instance) and exposes its read/write
APIs as a kernel service.

Mount order matters: the ``memory`` plugin must mount BEFORE this
plugin because this plugin injects it. The default ``Context``
config (in cortexm/__init__.py) handles this.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class StructuredHit:
    """One structured-tier retrieval result (a Fact).

    Mirrors the verbatim tier's VerbatimHit shape so the fusion
    bridge can rerank across both tiers uniformly.
    """
    fact_id: str
    subject: str
    relation: str
    value: str
    user_id: str
    source_tx_id: int | None
    valid_from: str
    valid_to: str | None
    confidence: float
    score: float           # retrieval score from the structured reader

    def to_dict(self) -> dict:
        return {
            "fact_id": self.fact_id,
            "subject": self.subject,
            "relation": self.relation,
            "value": self.value,
            "user_id": self.user_id,
            "source_tx_id": self.source_tx_id,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "confidence": self.confidence,
            "score": round(self.score, 4),
        }


class StructuredPlugin:
    """Adapter for the existing Memory class — kernel-mountable.

    Mount it on a kernel Context that already has a "memory" service
    (a fully-constructed Memory instance). The plugin does NOT own
    the Memory — it just forwards calls. The kernel's effect/cleanup
    contract still applies: on dispose, we close the Memory's DB
    connection if it owns it.

    Usage::

        from cortexm.kernel import Context
        from cortexm.api.memory import Memory
        from cortexm.plugins.structured import StructuredPlugin

        ctx = Context()
        mem = Memory()
        ctx.service("memory", mem)
        ctx.mount(StructuredPlugin())

        s = ctx.inject("structured")["structured"]
        s.add("Alice works at Google", user_id="alice")
        hits = s.search("Where does Alice work?", user_id="alice")
    """

    name = "structured"
    inject = ["memory"]

    def __init__(self, dispose_memory: bool = False) -> None:
        # If True, we own the Memory instance and will close its DB
        # on dispose. If False (default), the caller owns it.
        # Most callers construct the Memory themselves and pass it
        # in; they want to keep ownership. Set True for tests.
        self.dispose_memory = dispose_memory
        self._mem = None

    def apply(self, ctx) -> None:
        deps = ctx.inject("memory")
        self._mem = deps["memory"]
        ctx.service("structured", self)
        if self.dispose_memory:
            ctx.effect(self._close_memory)

    def _close_memory(self) -> None:
        if self._mem is None:
            return
        try:
            # Close the underlying TraceStore — safe even if Memory
            # doesn't expose it (the kernel is best-effort on dispose).
            store = getattr(self._mem, "store", None)
            if store is not None and hasattr(store, "close"):
                store.close()
        except Exception:
            pass

    # ---------------------------- write path ------------------------

    def add(self, text: str, *, user_id: str, agent_id: str | None = None,
            run_id: str | None = None, metadata: dict | None = None,
            timestamp=None) -> dict:
        """μ=0 ingest through the existing MemoryWriter.

        The Memory class already routes through PII guard, pattern
        extraction, contradiction resolution, palace indexing, and
        BLAKE3-chained audit log. This is a thin forwarder.
        """
        return self._mem.add(text, user_id=user_id, agent_id=agent_id,
                              run_id=run_id, metadata=metadata,
                              timestamp=timestamp)

    def edit(self, fact_id: str, new_text: str, *,
             edited_by: str = "user", reason: str = "") -> dict:
        """Human-in-the-loop correction with provenance stamping."""
        return self._mem.edit(fact_id, new_text, edited_by=edited_by,
                              reason=reason)

    # ---------------------------- read path -------------------------

    def search(self, query: str, *, user_id: str, k: int = 10,
               agent_id: str | None = None, run_id: str | None = None
               ) -> list[StructuredHit]:
        """Run the structured reader and adapt results to StructuredHit.

        The reader returns RetrievalResult objects with .facts and
        .score. We flatten to a list of StructuredHit for fusion
        with the verbatim tier.
        """
        raw = self._mem.search(query, user_id=user_id, limit=k,
                                agent_id=agent_id, run_id=run_id)
        # mem.search returns a DICT with a 'results' key (the list
        # of fact dicts) + extra context_block/provenance/relations.
        # Adapt both this shape and the legacy list shape.
        if isinstance(raw, dict):
            results = raw.get("results", [])
        elif isinstance(raw, (list, tuple)):
            results = raw
        else:
            results = []
        out: list[StructuredHit] = []
        for r in results:
            if isinstance(r, dict):
                out.append(StructuredHit(
                    fact_id=r.get("id", ""),
                    subject=r.get("subject", "") or r.get("memory", "")[:60],
                    relation=r.get("relation", "memory") or "memory",
                    value=r.get("memory", "") or r.get("value", ""),
                    user_id=r.get("user_id", user_id),
                    source_tx_id=r.get("source_tx_id") or r.get("source_id"),
                    valid_from=r.get("valid_from", "") or "",
                    valid_to=r.get("valid_to"),
                    confidence=float(r.get("confidence", 1.0) or 1.0),
                    score=float(r.get("score", 0.5) or 0.5),
                ))
            else:
                # RetrievalResult-like: has .facts list + .score
                facts = getattr(r, "facts", None) or []
                score = getattr(r, "score", 0.5) or 0.5
                for f in facts:
                    out.append(StructuredHit(
                        fact_id=getattr(f, "id", ""),
                        subject=getattr(f, "subject", ""),
                        relation=getattr(f, "relation", ""),
                        value=getattr(f, "value", ""),
                        user_id=getattr(f, "user_id", user_id),
                        source_tx_id=getattr(f, "source_tx_id", None)
                                      or getattr(f, "source_id", None),
                        valid_from=str(getattr(f, "valid_from", "") or ""),
                        valid_to=str(getattr(f, "valid_to", None) or "") or None,
                        confidence=float(getattr(f, "confidence", 1.0) or 1.0),
                        score=score,
                    ))
        # Sort by score desc, take top-k
        out.sort(key=lambda h: h.score, reverse=True)
        return out[:k]

    def structural_query(self, *, user_id: str, subject: str | None = None,
                         relation: str | None = None,
                         valid_at: str | None = None) -> list[StructuredHit]:
        """Direct Trace query bypassing VSA — for temporal/multi-hop.

        Wraps mem.store.find_facts / lifecycle.at helpers.
        """
        # The Memory class exposes structural queries via .search()
        # with temporal kwargs; we use those if available. Otherwise
        # fall back to a search query built from the subject+relation.
        if hasattr(self._mem, "structural_query"):
            return [StructuredHit(
                fact_id=r.get("id", ""), subject=r.get("subject", ""),
                relation=r.get("relation", ""), value=r.get("value", ""),
                user_id=r.get("user_id", user_id),
                source_tx_id=r.get("source_tx_id") or r.get("source_id"),
                valid_from=str(r.get("valid_from", "")),
                valid_to=r.get("valid_to"),
                confidence=float(r.get("confidence", 1.0) or 1.0),
                score=float(r.get("score", 1.0) or 1.0),
            ) for r in self._mem.structural_query(
                user_id=user_id, subject=subject, relation=relation,
                valid_at=valid_at)]
        # Fallback: synthesize a query and use search()
        synth_q = " ".join(filter(None, [subject or "", relation or ""]))
        return self.search(synth_q or subject or relation or "",
                            user_id=user_id, k=50)


__all__ = ["StructuredPlugin", "StructuredHit"]
