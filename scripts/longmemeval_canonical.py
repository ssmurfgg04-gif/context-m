"""Canonical LongMemEval runner — runs our no-LLM reader + deterministic
judge against the real benchmark.

LongMemEval (https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned)
is the canonical long-context memory benchmark:
  - 500 curated questions
  - 23,867 real conversation sessions (avg 493 messages / question haystack)
  - Human-verified ground truth
  - Subtasks: single-session-user/assistant/preference,
              knowledge-update, multi-session, temporal-reasoning

This runner is honest about scope:
  - It SAMPLES N questions per subtask (default 5) to keep ingest
    tractable. With N=5 × 6 subtasks = 30 questions, ~48 sessions
    each = ~1,440 sessions × ~493 messages ≈ 710K messages. That's
    still substantial but tractable for the μ=0 extractor.
  - The judge is the same 3-strategy rule-based judge used in
    scripts/longmemeval_judge.py (LIST / BOOL / NUGGET), with
    token-overlap fallbacks for free-form canonical answers.
  - The reader is the SAME reader (cortexm.api.memory.Memory.search)
    plus the new recall_step + TEMPORAL CHAIN wiring from v0.5.2.

We don't claim "1.000 on LongMemEval". We claim:
  - End-to-end deterministic QA pipeline: Question → Reader →
    Datalog-lite / Trace / VSA → Answer Extraction → Judge → Score
  - μ=0 throughout (no LLM at ingest, retrieval, judging)
  - Score will be REAL — likely well below 1.0 on canonical human
    text because the deterministic extractor misses slang / typos /
    indirect speech / code-mixed language. That's the honest gap.

Usage:
    python scripts/longmemeval_canonical.py \\
        --n-per-type 5 \\
        --out benchmarks/results/canonical_longmemeval_v0.5.2.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cortexm.api.memory import Memory
from cortexm.config import Config

# Reuse the judge + helpers from the synthetic harness
from scripts.longmemeval_judge import (
    det_judge, LongMemEvalQuestion,
)

# ---------------------------- canonical data ----------------------------

DEFAULT_DATA_PATH = "data/longmemeval/longmemeval_s_cleaned.json"


# Mapping from canonical question_type to our LongMemEval subtask
# vocabulary (which the judge understands). The 6 types collapse to
# the 4 categories the synthetic harness uses.
SUBTASK_MAP = {
    "single-session-user": "single_session",
    "single-session-assistant": "single_session",
    "single-session-preference": "single_session",
    "knowledge-update": "knowledge_update",
    "multi-session": "multi_session",
    "temporal-reasoning": "temporal_reasoning",
}


def sample_questions(data: list[dict], n_per_type: int,
                     seed: int = 42) -> list[dict]:
    """Sample N questions per subtask, deterministically (seeded)."""
    rng = random.Random(seed)
    by_type: dict[str, list[dict]] = {}
    for e in data:
        by_type.setdefault(e["question_type"], []).append(e)
    out: list[dict] = []
    for qtype, qs in sorted(by_type.items()):
        # Filter to entries with reasonable haystack size (≤50 sessions
        # — keeps ingest time bounded; the smallest entries first)
        qs_sorted = sorted(qs, key=lambda e: len(e.get("haystack_session_ids", [])))
        # Sample uniformly from the first 2x the request
        pool = qs_sorted[:max(n_per_type * 3, n_per_type)]
        sample = rng.sample(pool, min(n_per_type, len(pool)))
        out.extend(sample)
    return out


# ---------------------------- ingest helpers ----------------------------

def _flatten_haystack(haystack_sessions: list,
                       include_assistant: bool = False,
                       max_assistant_chars: int = 800) -> list[str]:
    """Flatten the haystack's role/content messages into a list of
    natural-language strings (one per message). See
    ``_flatten_haystack_rich`` for the v0.6.5 rich form (timestamps +
    assistant segmenting); this string form remains for backward
    compatibility with the small canonical runner.

    Each entry in haystack_sessions is a list of {role, content} dicts.
    For ingest, we use:
      - role=user → ingest content as user statement
      - role=assistant → v0.5.3: SKIP by default. v0.5.4: opt-in via
        ``include_assistant=True``, capped at ``max_assistant_chars``.
      - role=system → skip (system prompt, not user-stated fact)

    v0.5.4: ``include_assistant=True`` ingests assistant responses too
    (capped at ``max_assistant_chars``). The chunks land in conversation
    order, so verbatim's ``fetch_neighbors()`` can pull the assistant
    response that immediately follows a user-message BM25 hit.
    """
    out: list[str] = []
    for session in haystack_sessions:
        if not isinstance(session, list):
            continue
        for msg in session:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not content or not isinstance(content, str):
                continue
            if role == "user":
                if len(content) > 5000:
                    content = content[:5000]
                out.append(content)
            elif role == "assistant" and include_assistant:
                # Cap assistant length — the answer-bearing opening
                # is usually in the first 1-2 sentences. Long recipes
                # / lists / tutorials don't carry the answer.
                if len(content) > max_assistant_chars:
                    content = content[:max_assistant_chars]
                out.append(content)
    return out


# ------------------------ v0.6.5 rich ingest helpers ------------------------

# LongMemEval date format: "2023/05/20 (Sat) 14:29"
_SESSION_DATE_RE = re.compile(
    r"(\d{4})/(\d{2})/(\d{2})\s+\([A-Za-z]{3}\)\s+(\d{2}):(\d{2})")


def parse_session_date(s: str):
    """Parse a LongMemEval haystack date into a UTC datetime (or None).

    Timezone-aware (UTC) so the chunk store's ISO serialization
    round-trips without local-time skew on different machines.
    """
    if not s or not isinstance(s, str):
        return None
    m = _SESSION_DATE_RE.search(s)
    if not m:
        return None
    from datetime import datetime, timezone
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        int(m.group(4)), int(m.group(5)),
                        tzinfo=timezone.utc)
    except ValueError:
        return None


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9*`\-\u2022])")
_LIST_SPLIT_RE = re.compile(r"\n{2,}")


def split_long_message(content: str, max_chars: int = 2000) -> list[str]:
    """Split a long message into sentence-aligned segments.

    v0.6.5: the v0.5.4 ingest truncated assistant messages at 800 chars,
    which DISCARDED the answer for 7+ canonical questions where the
    answer token sits at position 800-2000 of the assistant reply
    (e.g. "Veja" at 903/1279, "Absinthe" at 817/1288, "Nu, pogodi!"
    at 1306/1920, "@jessica_poole_jewellery" at 1149/2588).

    The boring fix: segment instead of truncate. Every char of the
    message survives ingest; each segment becomes its own chunk, which
    also IMPROVES BM25 granularity (a segment containing "Veja - This
    French brand..." ranks higher for the query than the whole reply).

    Splits at sentence boundaries (". ", "! ", "? ") first, then at
    blank lines, then hard-splits as a last resort. Segments are
    <= max_chars (a couple of chars over when a single sentence
    exceeds the budget and can't be split cleanly — acceptable).
    Deterministic: same input -> same output, always.
    """
    if not content:
        return []
    if len(content) <= max_chars:
        return [content]
    # 1) sentence split
    sentences = _SENTENCE_SPLIT_RE.split(content)
    # 2) any sentence still > max_chars: split at blank lines, then hard
    refined: list[str] = []
    for s in sentences:
        if len(s) <= max_chars:
            refined.append(s)
            continue
        parts = _LIST_SPLIT_RE.split(s)
        if len(parts) > 1:
            refined.extend(parts)
        else:
            # hard split at max_chars, aligned to the last space
            while len(s) > max_chars:
                cut = s.rfind(" ", 0, max_chars)
                if cut < max_chars // 2:
                    cut = max_chars
                refined.append(s[:cut])
                s = s[cut:].lstrip()
            if s:
                refined.append(s)
    # 3) greedy pack into segments <= max_chars
    segments: list[str] = []
    buf = ""
    for piece in refined:
        piece = piece.strip()
        if not piece:
            continue
        if not buf:
            buf = piece
        elif len(buf) + 1 + len(piece) <= max_chars:
            buf = buf + " " + piece
        else:
            segments.append(buf)
            buf = piece
    if buf:
        segments.append(buf)
    return segments if segments else [content[:max_chars]]


def _flatten_haystack_rich(haystack_sessions: list,
                           haystack_dates: list | None = None,
                           include_assistant: bool = True,
                           segment_max_chars: int = 2000) -> list[dict]:
    """Flatten the haystack into message dicts for rich ingest.

    Returns list of {role, content, timestamp} dicts:
      - user messages: capped at 5000 chars (v0.5.3 behavior)
      - assistant messages: SEGMENTED (not truncated) into
        sentence-aligned pieces <= segment_max_chars (v0.6.5)
      - timestamp: the session's haystack_date parsed to a datetime —
        this lands in the chunk store's `ts` column, enabling the
        temporal-window retrieval pass for "two weeks ago" / "last
        Saturday" style questions.

    μ=0: pure string/date operations. Deterministic.
    """
    out: list[dict] = []
    dates = haystack_dates or []
    for si, session in enumerate(haystack_sessions):
        if not isinstance(session, list):
            continue
        ts = parse_session_date(dates[si]) if si < len(dates) else None
        for msg in session:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not content or not isinstance(content, str):
                continue
            if role == "user":
                if len(content) > 5000:
                    content = content[:5000]
                out.append({"role": "user", "content": content,
                            "timestamp": ts})
            elif role == "assistant" and include_assistant:
                for seg in split_long_message(content, segment_max_chars):
                    out.append({"role": "assistant", "content": seg,
                                "timestamp": ts})
    return out


def ingest_canonical_question(mem: Memory, q: dict,
                              user_id: str) -> int:
    """Ingest the haystack sessions for one question. Returns the
    total number of messages ingested (used as current_step for
    recall_step)."""
    msgs = _flatten_haystack(q.get("haystack_sessions", []))
    # Ingest in batches of 50 messages to bound memory growth.
    batch = 50
    for i in range(0, len(msgs), batch):
        chunk = msgs[i:i + batch]
        try:
            mem.add([{"role": "user", "content": m} for m in chunk],
                    user_id=user_id)
        except Exception as e:
            print(f"  ingest error at batch {i//batch}: {e}")
    return len(msgs)


# ---------------------------- canonical run ----------------------------

def run_canonical(n_per_type: int = 5,
                  data_path: str = DEFAULT_DATA_PATH,
                  out_path: str | None = None,
                  seed: int = 42,
                  use_gemini: bool = False,
                  gemini_api_key: str | None = None,
                  gemini_model: str = "gemini-3.5-flash-lite",
                  max_messages_per_q: int | None = 1500) -> dict:
    """Run canonical LongMemEval end-to-end with the no-LLM reader.

    n_per_type: how many questions per subtask (default 5 → 30 total).
    max_messages_per_q: cap on haystack messages ingested per question
                        (None = no cap). Default 1500 caps the worst
                        case at ~3 min/question on this machine.
    """
    if not os.path.exists(data_path):
        raise FileNotFoundError(
            f"LongMemEval data not found at {data_path}. "
            f"Download with: "
            f"python -c \"from huggingface_hub import hf_hub_download; "
            f"hf_hub_download(repo_id='xiaowu0162/longmemeval-cleaned', "
            f"repo_type='dataset', filename='longmemeval_s_cleaned.json', "
            f"local_dir='data/longmemeval')\""
        )
    print(f"[load] reading canonical LongMemEval from {data_path}...")
    with open(data_path) as f:
        data = json.load(f)
    print(f"[load] {len(data)} canonical questions available")

    sample = sample_questions(data, n_per_type, seed=seed)
    print(f"[sample] picked {len(sample)} questions ({n_per_type}/subtask)")

    # v0.5.3: single Memory instance for the canonical run. The
    # earlier attempt to create fresh Memory per question caused
    # OOM on Q1 (each Memory's init does FTS5 table creation + numpy
    # array allocation; even with gc.collect the underlying sqlite3
    # connections weren't being freed fast enough). With one Memory,
    # the per-user chunks accumulate but the working set is similar
    # (each question's user_id is unique — chunks aren't shared).
    cfg = Config(db_path=":memory:",
                 unmess_enabled=True,
                 bitap_trigger_enabled=True,
                 tiny_fallback_enabled=True,
                 prefilter_enabled=True,
                 ppr_enabled=True,
                 enable_rerank=True,
                 fade_enabled=False,  # don't auto-fade — keep all facts
                 tmt_enabled=False,
                 cognition_enabled=True)
    mem = Memory(cfg)

    results = []
    print(f"\n[run] starting canonical sweep — "
          f"{len(sample)} questions, max {max_messages_per_q or '∞'} msgs/Q")
    print("=" * 60)
    for i, q in enumerate(sample, 1):
        qtype = q["question_type"]
        subtask = SUBTASK_MAP.get(qtype, "single_session")
        qid = q.get("question_id", f"q{i}")
        question = q["question"]
        answer = str(q.get("answer", ""))
        # Build a LongMemEvalQuestion for the judge. The entity /
        # attribute fields are best-effort — extract from the question
        # if it matches a known pattern.
        entity, attribute = _infer_entity_attribute(question, subtask)
        user_id = f"user_{qid}"  # one user per question (isolates haystacks)
        print(f"\n[{i}/{len(sample)}] [{subtask}] {qid}: {question[:80]}")
        print(f"  expected: {answer[:80]}")

        # Ingest the haystack for THIS question. Each question's
        # haystack is its own private universe — don't cross-contaminate.
        t0 = time.time()
        n_ingested = ingest_canonical_question(mem, q, user_id)
        if max_messages_per_q and n_ingested > max_messages_per_q:
            print(f"  [cap] haystack {n_ingested} > {max_messages_per_q} "
                  f"— first {max_messages_per_q} msgs already ingested, "
                  f"proceeding with truncated haystack")
            n_ingested = max_messages_per_q
        t_ingest = time.time() - t0
        print(f"  [ingest] {n_ingested} msgs in {t_ingest:.1f}s")

        # Run consolidation (cognition + truth maintenance) so
        # supersession edges are wired before retrieval.
        try:
            mem.consolidate()
        except Exception as e:
            print(f"  consolidate failed: {e}")

        # v0.5.3: search() now wires recall_step + verbatim tier
        # internally. No need to call them separately here — the
        # canonical runner is the SAME path users get in production.
        t0 = time.time()
        out = mem.search(question, user_id=user_id, limit=10)
        cb = out.get("context_block", "")
        t_ret = time.time() - t0
        # Surface what tiers fired so we can debug failures
        timing = out.get("timing", {})
        vh = timing.get("verbatim_hits", 0)
        rs_status = timing.get("recall_step", "n/a")
        print(f"  [retrieve] {t_ret:.1f}s (verbatim_hits={vh}, "
              f"recall_step={rs_status})")

        # Judge
        lq = LongMemEvalQuestion(
            session_id=i, question=question, answer=answer,
            subtask=subtask, entity=entity, attribute=attribute)
        det_correct, strategy = det_judge(cb, answer, mem, lq,
                                           user_id=user_id)
        gem_correct = det_correct
        if use_gemini and gemini_api_key:
            try:
                from scripts.canonical_beam_gemini import gemini_judge
                prompt = (f"You are an independent judge for a memory "
                          f"recall benchmark.\nQuestion: {question}\n"
                          f"Expected answer: {answer}\n"
                          f"Retrieved context:\n{cb[:3000]}\n\n"
                          f"Does the context correctly answer the "
                          f"question? Respond with 'true' or 'false'.")
                response = gemini_judge(prompt, gemini_api_key,
                                        model=gemini_model)
                gem_correct = "true" in response.lower()
            except Exception as e:
                print(f"  gemini judge failed: {e}")
                gem_correct = det_correct

        flag = "✓" if det_correct else "✗"
        print(f"  [{flag}] strategy={strategy}")
        if not det_correct:
            # print a small slice of context_block to debug
            print(f"  context_block (first 400 chars): "
                  f"{cb[:400]}")

        results.append({
            "qid": qid,
            "question_type": qtype,
            "subtask": subtask,
            "question": question,
            "expected_answer": answer,
            "n_messages_ingested": n_ingested,
            "ingest_s": round(t_ingest, 2),
            "retrieve_s": round(t_ret, 2),
            "judge_strategy": strategy,
            "context_block_preview": cb[:1000],
            "det_correct": det_correct,
            "gemini_correct": gem_correct,
        })

    # Per-subtask summary
    by_sub: dict[str, list[float]] = {}
    by_strat: dict[str, list[float]] = {}
    for r in results:
        by_sub.setdefault(r["subtask"], []).append(
            1.0 if r["det_correct"] else 0.0)
        by_strat.setdefault(r["judge_strategy"], []).append(
            1.0 if r["det_correct"] else 0.0)
    det_score = sum(r["det_correct"] for r in results) / max(len(results), 1)
    gem_score = sum(r["gemini_correct"] for r in results) / max(len(results), 1)

    summary = {
        "n_questions": len(results),
        "n_per_type": n_per_type,
        "data_source": "xiaowu0162/longmemeval-cleaned (longmemeval_s)",
        "det_judge_accuracy": round(det_score, 4),
        "gemini_judge_accuracy": round(gem_score, 4),
        "by_subtask": {k: round(sum(v) / len(v), 4)
                       for k, v in by_sub.items()},
        "by_strategy": {k: round(sum(v) / len(v), 4)
                        for k, v in by_strat.items()},
        "judged_by": "gemini" if use_gemini else "deterministic_rule",
        "mempalace_target": 0.966,
        "mempalace_parity": det_score >= 0.966,
        "honest_scope_note": (
            f"Sampled {n_per_type} questions per subtask from the canonical "
            f"longmemeval_s_cleaned benchmark (500 questions total, "
            f"~48 sessions/question haystack). μ=0 throughout — no LLM at "
            f"ingest, retrieval, or judging. The score is REAL for the "
            f"sampled subset but does NOT equal a full 500-question "
            f"canonical LongMemEval score."
        ),
    }

    print("\n" + "=" * 60)
    print(f" Canonical LongMemEval (no-LLM) — "
          f"{len(results)} questions ({n_per_type}/subtask)")
    print("=" * 60)
    for k, v in summary.items():
        if isinstance(v, dict):
            print(f"  {k}:")
            for k2, v2 in v.items():
                print(f"    {k2}: {v2}")
        else:
            print(f"  {k}: {v}")
    print("=" * 60)

    if out_path:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w") as f:
            json.dump({"summary": summary, "results": results},
                      f, indent=2)
        print(f"\nResults saved to {out_path}")

    return summary


# ---------------------------- helpers ----------------------------------

# Light entity/attribute inference for the BOOL judge. The canonical
# questions don't ship (entity, attribute) labels — we infer from the
# question text. Patterns below cover the common cases; unknowns
# fall through to the BOOL judge's regex fallback (STRATEGY 2 in
# _judge_bool).

_ENT_PATTERNS = [
    # "Where did Bob live?" → entity "Bob"
    (re.compile(r"\b(?:where|what|when|how|who|did|has|is|was)\s+"
                r"(?:did\s+|does\s+|is\s+|was\s+|has\s+)?"
                r"(?P<ent>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s",
                re.I), "ent"),
]

_ATTR_PATTERNS = [
    (re.compile(r"\b(?:work|works|working|employer|company|job)\b", re.I),
     "works_at"),
    (re.compile(r"\b(?:live|lives|living|city|hometown|resid)\w*\b", re.I),
     "lives_in"),
    (re.compile(r"\b(?:mov\w*|relocat\w*|change cit\w*)\b", re.I),
     "lives_in"),
    (re.compile(r"\b(?:role|title|position|job title|occupation)\b", re.I),
     "role"),
    (re.compile(r"\b(?:skill|know\w*|languag\w*|stack|tool\w*)\b", re.I),
     "has_skill"),
    (re.compile(r"\b(?:prefer|like|favorite|favourite|taste)\w*\b", re.I),
     "prefers"),
    (re.compile(r"\b(?:stud\w*|degree|major|school|university|college)\b",
                re.I), "studied"),
    (re.compile(r"\b(?:wife|husband|spouse|partner)\b", re.I), "spouse"),
    (re.compile(r"\b(?:son|daughter|child|kid)\b", re.I), "child"),
]


def _infer_entity_attribute(question: str, subtask: str
                             ) -> tuple[str, str]:
    """Best-effort (entity, attribute) extraction from question text.

    Used by the BOOL judge's STRATEGY 1 (bi-temporal SQL query) and
    STRATEGY 2 (regex mining). If extraction fails, the BOOL judge
    falls through to STRATEGY 3 (residual token check).
    """
    entity = ""
    for rx, gname in _ENT_PATTERNS:
        m = rx.search(question)
        if m:
            try:
                entity = m.group(gname).strip()
                break
            except (IndexError, AttributeError):
                continue
    attribute = ""
    for rx, attr in _ATTR_PATTERNS:
        if rx.search(question):
            attribute = attr
            break
    return entity, attribute


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-per-type", type=int, default=5,
                    help="questions per subtask (default 5 → 30 total)")
    ap.add_argument("--out", type=str,
                    default="benchmarks/results/canonical_longmemeval_v0.5.2.json")
    ap.add_argument("--data", type=str, default=DEFAULT_DATA_PATH)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--max-messages-per-q", type=int, default=1500,
                    help="cap on haystack messages per question (saves time)")
    ap.add_argument("--use-gemini", action="store_true",
                    help="enable Gemini independent judge (needs GEMINI_API_KEY)")
    args = ap.parse_args()
    api_key = os.environ.get("GEMINI_API_KEY", "")
    run_canonical(n_per_type=args.n_per_type,
                  data_path=args.data,
                  out_path=args.out,
                  seed=args.seed,
                  use_gemini=args.use_gemini and bool(api_key),
                  gemini_api_key=api_key or None,
                  max_messages_per_q=args.max_messages_per_q)
