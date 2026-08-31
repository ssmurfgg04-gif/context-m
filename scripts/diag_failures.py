"""Diagnose failing canonical LongMemEval questions.

Runs a single question end-to-end exactly like the full runner does,
then reports:
  1. WHERE the answer lives in the raw haystack (which session, which
     message, user or assistant, at what char position)
  2. WHETHER that message was actually ingested (truncation / assistant
     cap / max_messages cap checks)
  3. WHAT the reader retrieved (full context block, not the 1000-char
     preview) — was the answer-bearing chunk in it?
  4. WHAT the judge did with it
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cortexm.api.memory import Memory
from cortexm.config import Config
from scripts.longmemeval_judge import det_judge, LongMemEvalQuestion
from scripts.longmemeval_canonical import (
    SUBTASK_MAP, _infer_entity_attribute, _flatten_haystack,
)

DATA = "benchmarks/data/longmemeval/longmemeval_s_cleaned.json"


def find_answer_locations(q, answer):
    """Locate the expected answer inside the raw haystack."""
    a = str(answer).strip()
    # Build search needles: the full answer, plus its distinctive tokens
    needles = [a.lower()]
    # Strip parenthetical: "University of California, Los Angeles (UCLA)"
    m = re.match(r"^(.*?)\s*\((.*?)\)\s*$", a)
    if m:
        needles.append(m.group(1).lower())
        needles.append(m.group(2).lower())
    # Numeric answers: also search bare number
    num = re.sub(r"[$,]", "", a)
    if num != a.lower():
        needles.append(num)
    hits = []
    for si, session in enumerate(q.get("haystack_sessions", [])):
        if not isinstance(session, list):
            continue
        for mi, msg in enumerate(session):
            if not isinstance(msg, dict):
                continue
            content = msg.get("content", "") or ""
            lc = content.lower()
            for nd in needles:
                if nd in lc:
                    pos = lc.find(nd)
                    hits.append({
                        "session_idx": si,
                        "msg_idx": mi,
                        "role": msg.get("role"),
                        "pos": pos,
                        "len": len(content),
                        "around": content[max(0, pos - 120):pos + 160].replace("\n", " "),
                    })
                    break
    return hits


def diagnose(qid_prefix: str, run_retrieval: bool = True):
    data = json.load(open(DATA))
    q = None
    for e in data:
        if e.get("question_id", "").startswith(qid_prefix):
            q = e
            break
    if q is None:
        print(f"[!] qid {qid_prefix} not found")
        return
    qid = q["question_id"]
    question = q["question"]
    answer = str(q.get("answer", ""))
    subtask = SUBTASK_MAP.get(q["question_type"], "?")
    print("=" * 90)
    print(f"QID {qid} [{q['question_type']}/{subtask}]")
    print(f"Q: {question}")
    print(f"EXPECTED: {answer}")
    print(f"sessions: {len(q.get('haystack_session_ids', []))}")

    # 1. Where does the answer live?
    hits = find_answer_locations(q, answer)
    print(f"\n[GROUND TRUTH] answer appears in {len(hits)} haystack message(s):")
    for h in hits[:8]:
        print(f"  - session#{h['session_idx']} msg#{h['msg_idx']} ({h['role']}) "
              f"pos={h['pos']}/{h['len']}: ...{h['around']}...")

    # 2. What would _flatten_haystack ingest?
    flat = _flatten_haystack(q.get("haystack_sessions", []), include_assistant=True)
    flat_user_only = _flatten_haystack(q.get("haystack_sessions", []), include_assistant=False)
    print(f"\n[FLATTEN] ingestable msgs: {len(flat)} (assistant included), "
          f"{len(flat_user_only)} (user only)")
    # check each answer hit survives flattening (assistant capped at 800)
    for h in hits[:8]:
        if h["role"] == "assistant" and h["pos"] > 800:
            print(f"  [!!] answer at pos {h['pos']} in an ASSISTANT msg "
                  f"of len {h['len']} — TRUNCATED by the 800-char cap!")
    if len(flat) > 2000:
        print(f"  [!!] haystack {len(flat)} > 2000 cap — messages past 2000 dropped")

    if not run_retrieval:
        return

    # 3. Run the actual pipeline
    cfg = Config(
        db_path="/tmp/cortexm_diag/diag.db",
        unmess_enabled=True, bitap_trigger_enabled=True,
        tiny_fallback_enabled=True, prefilter_enabled=True,
        ppr_enabled=True, enable_rerank=True, fade_enabled=False,
        tmt_enabled=False, cognition_enabled=True,
    )
    os.makedirs("/tmp/cortexm_diag", exist_ok=True)
    for suf in ("", "-wal", "-shm", "-journal"):
        try:
            os.unlink("/tmp/cortexm_diag/diag.db" + suf)
        except FileNotFoundError:
            pass
    mem = Memory(cfg)
    q_user_id = f"user_{qid}"
    t0 = time.time()
    n_ing = 0
    batch = 50
    for i in range(0, len(flat[:2000]), batch):
        chunk = flat[i:i + batch]
        try:
            mem.add([{"role": "user", "content": m} for m in chunk],
                    user_id=q_user_id)
        except Exception as e:
            print(f"  ingest error: {e}")
        n_ing += len(chunk)
    try:
        mem.consolidate()
    except Exception as e:
        print(f"  consolidate failed: {e}")
    print(f"\n[INGEST] {n_ing} msgs in {time.time()-t0:.1f}s")

    out = mem.search(question, user_id=q_user_id, limit=10)
    cb = out.get("context_block", "")
    vh = out.get("timing", {}).get("verbatim_hits", 0)

    # aggregation enrichment, same as the full runner
    from scripts.longmemeval_canonical_full import (
        _is_aggregation_question, _enrich_with_aggregation_chunks,
    )
    if _is_aggregation_question(question):
        cb, n_agg = _enrich_with_aggregation_chunks(mem, cb, question, q_user_id)
        print(f"[AGG] enrichment added {n_agg} topic chunks")
        vh += n_agg

    print(f"[RETRIEVE] verbatim_hits={vh}, context_block={len(cb)} chars")
    ans_lc = answer.lower()
    m = re.match(r"^(.*?)\s*\((.*?)\)\s*$", ans_lc)
    present = ans_lc in cb.lower()
    if not present and m:
        present = m.group(1) in cb.lower() or m.group(2) in cb.lower()
    num = re.sub(r"[$,]", "", ans_lc)
    if not present and num != ans_lc and re.fullmatch(r"[\d.]+", num):
        present = num in cb.lower()
    print(f"[ANSWER-IN-CONTEXT] {present}")
    if not present:
        print("  -> answer NOT in context block: retrieval miss (or ingest truncation)")

    print("\n[FULL CONTEXT BLOCK]")
    print(cb[:4000])
    if len(cb) > 4000:
        print(f"... ({len(cb) - 4000} more chars)")

    # 4. Judge
    entity, attribute = _infer_entity_attribute(question, subtask)
    lq = LongMemEvalQuestion(session_id=0, question=question, answer=answer,
                             subtask=subtask, entity=entity, attribute=attribute)
    ok, strategy = det_judge(cb, answer, mem, lq, user_id=q_user_id)
    print(f"\n[JUDGE] correct={ok} strategy={strategy}")
    try:
        mem.close()
    except Exception:
        pass


if __name__ == "__main__":
    qid = sys.argv[1] if len(sys.argv) > 1 else "d851d5ba"
    no_ret = "--no-retrieval" in sys.argv
    diagnose(qid, run_retrieval=not no_ret)
