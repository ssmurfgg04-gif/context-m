"""Composable ingestion pipelines (Cognee learn).

Reddit deep-dive 2026-08-29 — Cognee's "Pipelines" module
structured ingestion as composable stages:

    chunk → embed → extract → graph → index

Cognee's insight: monolithic `mem.add(text)` does everything
in one pass; that's fine for chat (you want low latency) but
bad for batch (you want to control chunk size, embedder codec,
graph construction rules, etc.).

cortexm.Pipeline is the lean answer. It's a declarable list of
stages; each stage is a callable ``stage(documents, ctx) ->
documents``. The pipeline runs them in order, threading a
PipelineContext through so stages can share state.

Built-in stages:
  - ``Chunk(max_tokens=512, overlap=64)``  — split long docs
  - ``Extract(patterns="default")``         — μ=0 extractor
  - ``Embed(codec="int8")``                 — VSA hologram encode
  - ``Index(backend="quadrant")``           — palace.add_batch
  - ``Dedup(threshold=0.92)``               — value-match dedup
  - ``Audit(stage="…")``                    — log a stage marker

Usage::

    from cortexm import Memory, Pipeline
    from cortexm.pipeline import stages

    m = Memory()
    pipe = Pipeline([
        stages.Chunk(max_tokens=512, overlap=64),
        stages.Extract(),
        stages.Embed(codec="int8"),
        stages.Index(),
    ], memory=m)
    pipe.run(["long doc 1 ...", "long doc 2 ..."])

Lean: ~250 LoC, no new deps. Stages are plain callables; users
can write their own (e.g. an LLM-enrichment stage for μ>0 paths).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


# ----------------------------- PipelineContext -----------------------------

@dataclass
class PipelineContext:
    """Threaded through every stage. Stages read/write state here.

    Fields:
      - memory: the live Memory object (stages call memory.add /
        memory.writer.ingest_candidates / memory.palace.add_batch)
      - user_id, agent_id, run_id: scope — every chunk the pipeline
        produces inherits these
      - stats: per-stage invocation count + last-run timing
      - stage_state: scratch dict for inter-stage handoff (e.g.
        Chunk() writes 'chunks', Extract() reads 'chunks' and writes
        'candidates', Index() reads 'candidates')
    """
    memory: Any = None
    user_id: str = "default"
    agent_id: str | None = None
    run_id: str | None = None
    stats: dict = field(default_factory=dict)
    stage_state: dict = field(default_factory=dict)

    def bump(self, stage_name: str, **kw) -> None:
        s = self.stats.setdefault(stage_name, {"calls": 0, "last": {}})
        s["calls"] += 1
        s["last"] = kw


# ----------------------------- Stage contract ------------------------------

class Stage:
    """Base class for pipeline stages. Subclasses implement __call__."""
    name: str = "stage"

    def __call__(self, ctx: PipelineContext,
                 documents: Iterable[str]) -> list[str]:
        raise NotImplementedError


# ----------------------------- Built-in stages -----------------------------

class Chunk(Stage):
    """Split long documents into ≤max_tokens chunks. Overlap so
    facts that span a boundary don't get lost.

    Tokenization: a lean regex word splitter — no transformer tokenizer
    dependency (μ=0 stays intact)."""
    name = "chunk"

    def __init__(self, max_tokens: int = 512, overlap: int = 64):
        self.max_tokens = max(max_tokens, 16)
        self.overlap = max(0, min(overlap, self.max_tokens // 4))

    def __call__(self, ctx, documents):
        out: list[str] = []
        for doc in documents:
            toks = re.findall(r"\S+|\s+", doc)  # word + whitespace
            i = 0
            while i < len(toks):
                chunk_toks = toks[i:i + self.max_tokens]
                chunk = "".join(chunk_toks).strip()
                if chunk:
                    out.append(chunk)
                if i + self.max_tokens >= len(toks):
                    break
                i += self.max_tokens - self.overlap
        ctx.stage_state["chunks"] = out
        ctx.bump(self.name, n_chunks=len(out))
        return out


class Extract(Stage):
    """Run the μ=0 deterministic extractor over each chunk. Emits
    candidate (subject, relation, value) triples into
    ``ctx.stage_state['candidates']`` — does NOT commit them yet
    (Index() does that so dedup/rbac can run first)."""
    name = "extract"

    def __init__(self, patterns: str = "default"):
        self.patterns = patterns

    def __call__(self, ctx, documents):
        mem = ctx.memory
        if mem is None:
            ctx.stage_state["candidates"] = []
            return list(documents)
        extractor = mem.extractor
        candidates = []
        for chunk in documents:
            try:
                facts = extractor.extract(chunk)
            except Exception:
                facts = []
            for f in facts:
                # keep the Candidate objects as-is — Index() passes
                # them straight to writer.ingest_candidates which
                # expects Candidate dataclass instances, not dicts.
                candidates.append(f)
        ctx.stage_state["candidates"] = candidates
        ctx.bump(self.name, n_candidates=len(candidates))
        return list(documents)


class Embed(Stage):
    """Encode every candidate's subject/relation/value into VSA
    holograms. Stores the vectors in ctx.stage_state['vectors']
    keyed by candidate index; Index() consumes them."""
    name = "embed"

    def __init__(self, codec: str = "int8"):
        self.codec = codec

    def __call__(self, ctx, documents):
        mem = ctx.memory
        if mem is None:
            return list(documents)
        candidates = ctx.stage_state.get("candidates", [])
        palace = mem.palace
        vecs = []
        for c in candidates:
            try:
                v = palace.vsa.encode_fact(
                    palace.embedder.embed(c["subject"]),
                    palace.embedder.embed(c["relation"]),
                    palace.embedder.embed(c["value"]))
                vecs.append(v)
            except Exception:
                vecs.append(None)
        ctx.stage_state["vectors"] = vecs
        ctx.bump(self.name, n_vectors=len(vecs))
        return list(documents)


class Index(Stage):
    """Commit candidates to the Trace + Palace via the standard
    MemoryWriter.ingest_candidates path — same quarantine / lifecycle
    / palace / edges pipeline as pattern-extracted facts. Provenance
    records ``pipeline`` origin so audits can distinguish pipeline-
    ingested facts from chat-ingested ones."""
    name = "index"

    def __init__(self, backend: str | None = None):
        # backend is informational only — palace already chose its
        # backend via Config.index_backend. We expose it on the stage
        # so the pipeline declaration documents the active backend.
        self.backend = backend

    def __call__(self, ctx, documents):
        mem = ctx.memory
        if mem is None:
            return list(documents)
        candidates = ctx.stage_state.get("candidates", [])
        if not candidates:
            ctx.bump(self.name, committed=0)
            return list(documents)
        try:
            mem.writer.ingest_candidates(
                candidates, user_id=ctx.user_id, agent_id=ctx.agent_id,
                source="pipeline")
        except Exception:
            # fall back to per-doc mem.add
            for doc in documents:
                try:
                    mem.add(doc, user_id=ctx.user_id,
                            agent_id=ctx.agent_id, run_id=ctx.run_id)
                except Exception:
                    pass
        mem.reader.invalidate_caches()
        ctx.bump(self.name, committed=len(candidates))
        return list(documents)


class Dedup(Stage):
    """Value-match dedup: drop candidates whose (subject, relation)
    pair already has a near-identical value (similarity ≥ threshold)
    earlier in the candidate list. Cuts redundant writes."""
    name = "dedup"

    def __init__(self, threshold: float = 0.92):
        self.threshold = float(threshold)

    def __call__(self, ctx, documents):
        candidates = ctx.stage_state.get("candidates", [])
        from cortexm.util import similarity
        kept = []
        seen: dict[tuple, list[str]] = {}
        for c in candidates:
            subj = getattr(c, "subject", "") or ""
            rel = getattr(c, "relation", "") or ""
            val = getattr(c, "value", "") or ""
            key = (subj.lower(), rel.lower())
            dup = False
            for prev_val in seen.get(key, []):
                if similarity(prev_val, val) >= self.threshold:
                    dup = True
                    break
            if not dup:
                kept.append(c)
                seen.setdefault(key, []).append(val)
        ctx.stage_state["candidates"] = kept
        ctx.bump(self.name, kept=len(kept),
                 dropped=len(candidates) - len(kept))
        return list(documents)


class Audit(Stage):
    """Insert a marker into the audit log so the trajectory viewer
    can render pipeline progress. Lean: just calls memory.audit_log.log."""
    name = "audit"

    def __init__(self, stage: str = "pipeline"):
        self.stage = stage

    def __call__(self, ctx, documents):
        mem = ctx.memory
        if mem is not None:
            try:
                mem.audit_log.log(f"pipeline.{self.stage}",
                                   resource=ctx.user_id,
                                   meta={"n_docs": len(list(documents))})
            except Exception:
                pass
        ctx.bump(self.name)
        return list(documents)


# ----------------------------- Pipeline -----------------------------------

class Pipeline:
    """A declarable ingestion pipeline.

        pipe = Pipeline([stages.Chunk(...), stages.Extract(),
                         stages.Embed(), stages.Index()],
                        memory=m, user_id="alice")
        pipe.run(["doc 1 ...", "doc 2 ..."])

    Stages run in order, each receiving the output of the previous
    stage (default: pass-through — most stages communicate via
    ctx.stage_state, not the return value).
    """
    def __init__(self, stages: list[Stage], *, memory=None,
                 user_id: str = "default",
                 agent_id: str | None = None,
                 run_id: str | None = None):
        self.stages = stages
        self.ctx = PipelineContext(memory=memory, user_id=user_id,
                                    agent_id=agent_id, run_id=run_id)

    def run(self, documents: Iterable[str]) -> dict:
        docs = list(documents)
        for stage in self.stages:
            try:
                docs = list(stage(self.ctx, docs) or docs)
            except Exception as e:
                self.ctx.bump(stage.name, error=str(e)[:200])
                # continue — one bad stage shouldn't kill the pipeline
        return {
            "n_in": len(docs),
            "stats": self.ctx.stats,
            "stage_state_keys": list(self.ctx.stage_state.keys()),
        }


# Convenience namespace
stages = type("stages", (), {
    "Chunk": Chunk,
    "Extract": Extract,
    "Embed": Embed,
    "Index": Index,
    "Dedup": Dedup,
    "Audit": Audit,
})
