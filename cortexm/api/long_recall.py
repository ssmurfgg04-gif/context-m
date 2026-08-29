"""Long-context recall — "memory past 20 steps".

This is the killer feature the user asked for explicitly:

  > "use my token to submit the core of whatever a user needs
  > should be the core attempt to get memory and recall past 20 steps"

Problem: every mainstream LLM has a context window that scrolls.
After ~20 turns of conversation (~8k tokens for a 32k-window model),
the early turns have scrolled out of the prompt. The LLM forgets
what was said at turn 1 by turn 25. This is the #1 pain point for
anyone building agents on top of an LLM — the model literally
cannot remember what it committed to two turns ago, let alone last
session.

cortexm's answer:

  Every fact the extractor derives from each turn is persisted to
  the bi-temporal Trace with a step number (run_id encodes the
  session, agent_id encodes the agent, the chunk's `created_at`
  encodes the step order). On retrieval, we DON'T just query by
  relevance — we BIAS toward facts whose step is about to scroll
  out of the LLM's window. A fact from step 5 in a 30-turn
  conversation is far more valuable to surface now than a fact from
  step 28 — the LLM still has step 28 in its prompt.

  This is the asymmetric retrieval insight: the closer a fact is to
  scrolling out, the more we should boost it. The standard cosine
  score ranks by similarity; we multiply by a step-distance decay
  that peaks at the LLM's window edge.

Concrete API:

  m.recall_step(query, user_id="alice", current_step=30,
                 window=20, k=12)

  → returns the top-k facts RELEVANT to the query AND in danger of
    scrolling out of the LLM's window.

  m.stepped_context_block(query, user_id="alice", current_step=30,
                           window=20, k=12)

  → returns a ready-to-inject markdown context block:

      ## Recalled memory (steps 1–10 about to scroll out)
      - **[step 5]** Alice works at Google  (conf=0.92, valid_from=2026-08-01)
      - **[step 8]** Alice's birthday is 1990-05-12  (conf=0.88)
      - **[step 11]** Alice prefers Python  (conf=0.85)
      ...

This is the "memory past 20 steps" UX. Drop it into your agent's
system prompt template, and the LLM never forgets — even if it
physically scrolled the early turns out of its window.

Lean and simple: ~200 LoC, pure Python, μ=0 (no LLM, deterministic
step-distance decay multiplied onto the existing VSA fusion score).
"""
from __future__ import annotations

import datetime as _dt
import math
from datetime import datetime, timezone
from typing import Any

from cortexm.util import parse_ts


def _step_for_fact(fact, store) -> int | None:
    """Best-effort step number for a fact. We use chunk.created_at
    ordinal within the (user_id, agent_id, run_id) scope — the order
    in which the chunk entered the Trace. If the store doesn't expose
    a step field directly, we synthesize one from created_at ordering.

    Returns None if no chunk is attached (machine-derived facts
    with no source — these don't have a meaningful step number).
    """
    if not fact.source_id:
        return None
    try:
        chunk = store.get_chunk(fact.source_id)
        if not chunk:
            return None
        # if the schema already has a step column, use it
        step = chunk.get("step") if isinstance(chunk, dict) else None
        if step is not None:
            return int(step)
        # otherwise use created_at ordering — query the store for
        # the rank of this chunk's created_at among same-scope chunks
        ca = chunk.get("created_at") if isinstance(chunk, dict) else None
        if not ca:
            return None
        try:
            row = store.conn.execute(
                "SELECT COUNT(*) AS n FROM chunks "
                "WHERE user_id=? AND created_at < ?",
                (fact.user_id, ca)).fetchone()
            return int(row["n"]) + 1 if row else None
        except Exception:
            return None
    except Exception:
        return None


def _step_distance_boost(step: int | None, current_step: int,
                          window: int) -> float:
    """Asymmetric retrieval: boost facts close to scrolling out of
    the LLM's window.

    If current_step = 30 and window = 20, the LLM still sees steps
    11..30. Step 10 is ABOUT to scroll out — boost it most. Step 1
    has already scrolled out — boost it strongly too. Step 25 is
    still in the LLM's prompt — boost it less (the LLM already has it).

    The boost function is a Gaussian centered at (current_step -
    window), so it peaks at the window edge:

        boost(step) = 1 + peak * exp(-(step - (current_step - window))^2
                                      / (2 * sigma^2))

    Plus a constant floor of 1.0 (we never zero-out a fact — the
    underlying VSA score still ranks it; we only nudge ordering).
    """
    if step is None:
        return 1.0  # no step info → no boost, no penalty
    if current_step <= 0 or window <= 0:
        return 1.0
    edge = max(0, current_step - window)
    sigma = max(1.0, window / 3.0)  # spread = 1/3 of window
    peak = 0.6  # max +60% boost at the window edge
    # Gaussian centered at the edge, but also weighted for already-
    # scrolled-out facts (step < edge) — they get full peak boost
    # because the LLM has zero access to them now.
    if step <= edge:
        return 1.0 + peak
    return 1.0 + peak * math.exp(
        -((step - edge) ** 2) / (2 * sigma * sigma))


def recall_step(memory, query: str, *, user_id: str | None = None,
                agent_id: str | None = None, run_id: str | None = None,
                current_step: int = 0, window: int = 20,
                k: int = 12) -> dict:
    """Asymmetric retrieval: top-k facts RELEVANT to the query AND in
    danger of scrolling out of the LLM window.

    Pipeline:
      1. Run the standard reader.search() — produces top-K candidates
         ranked by VSA + symbolic fusion.
      2. For each candidate fact, compute the step-distance boost.
      3. Re-rank by (fusion_score * boost). Top-k wins.

    Returns a dict shaped like ``m.search()`` but with extra fields
    per memory:
      - step: int | None
      - step_distance_boost: float
      - scrolled_out: bool  (True if step <= current_step - window)
    """
    user_id = user_id or memory.config.default_user_id
    # over-fetch then re-rank — we want enough candidates so the
    # step-distance re-ordering has room to surface scrolled-out facts
    raw_k = max(k * 4, 24)
    result = memory.reader.search(query, user_id=user_id, agent_id=agent_id,
                                   run_id=run_id, k=raw_k)
    scored: list[tuple[float, Any, int | None, float, bool]] = []
    for f in result.facts:
        step = _step_for_fact(f, memory.store)
        boost = _step_distance_boost(step, current_step, window)
        # underlying fusion score (cosine + symbolic + chunk_recall)
        fs = float(getattr(f, "score", 0.0) or
                   getattr(f, "fusion_score", 0.0) or 0.0)
        # if the reader didn't attach a score, fall back to confidence
        if fs <= 0.0:
            fs = float(getattr(f, "confidence", 0.0) or 0.0)
        new_score = fs * boost
        scrolled_out = step is not None and current_step > 0 and step <= (
            current_step - window)
        scored.append((new_score, f, step, boost, scrolled_out))
    scored.sort(key=lambda x: -x[0])
    top = scored[:k]

    out_memories = []
    for new_score, f, step, boost, scrolled_out in top:
        out_memories.append({
            "id": f.id,
            "memory": f"{f.subject} | {f.relation} | {f.value}",
            "step": step,
            "step_distance_boost": round(boost, 3),
            "scrolled_out": scrolled_out,
            "fusion_score": round(new_score, 4),
            "confidence": float(getattr(f, "confidence", 0.0) or 0.0),
            "valid_from": str(f.valid_from) if f.valid_from else None,
            "valid_to": str(f.valid_to) if f.valid_to else None,
            "source_snippet": _snippet(memory, f),
        })
    return {
        "query": query,
        "user_id": user_id,
        "current_step": current_step,
        "window": window,
        "results": out_memories,
        "context_block": _format_context_block(
            out_memories, current_step, window),
        "llm_calls": 0,
    }


def _snippet(memory, f) -> str:
    if not f.source_id:
        return ""
    chunk = memory.store.get_chunk(f.source_id)
    if chunk and chunk.get("text"):
        return chunk["text"][:160]
    return ""


def _format_context_block(mems: list[dict], current_step: int,
                          window: int) -> str:
    """Render a markdown context block ready to inject into the LLM
    system prompt. Grouped by 'scrolled out' vs 'in window' so the
    model sees the structure clearly."""
    if not mems:
        return ""
    edge = max(0, current_step - window)
    scrolled = [m for m in mems if m["scrolled_out"]]
    in_win = [m for m in mems if not m["scrolled_out"]]
    lines = []
    if scrolled:
        lines.append(f"## Recalled memory (steps 1–{edge} — scrolled "
                     f"out of context window)")
        for m in scrolled:
            lines.append(f"- **[step {m['step']}]** {m['memory']}  "
                         f"(conf={m['confidence']:.2f}, "
                         f"valid_from={m.get('valid_from', 'n/a')})")
            if m.get("source_snippet"):
                lines.append(f"  > …{m['source_snippet'][:120]}")
    if in_win:
        lines.append(f"\n## Active memory (steps {edge+1}–{current_step} "
                     f"— still in window, surfaced for relevance)")
        for m in in_win:
            lines.append(f"- **[step {m['step']}]** {m['memory']}  "
                         f"(conf={m['confidence']:.2f})")
    return "\n".join(lines)


def stepped_context_block(memory, query: str, *,
                           user_id: str | None = None,
                           current_step: int = 0,
                           window: int = 20, k: int = 12) -> str:
    """Convenience wrapper — returns just the context_block string."""
    return recall_step(memory, query, user_id=user_id,
                       current_step=current_step, window=window,
                       k=k)["context_block"]
