"""Re-run the 21 real v0.6.4 failures through the NEW v0.6.5 pipeline.

Uses the exact runner code path (longmemeval_canonical_full._run_one_question)
so what we measure here is what the 20-shard GitHub run will measure.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.longmemeval_canonical_full import _run_one_question, _make_config

DATA = "benchmarks/data/longmemeval/longmemeval_s_cleaned.json"

FAILS = [
    # multi_session (7)
    'gpt4_f2262a51', '1f2b8d4f', 'e25c3b8d', 'bb7c3b45', '60159905',
    'ef9cf60a', 'd6062bb9',
    # temporal (6)
    'gpt4_e061b84g', 'gpt4_d6585ce9', '0bc8ad93', 'gpt4_8279ba03',
    'cc6d1ec1', 'd01c6aa8',
    # knowledge_update (1)
    '0977f2af',
    # single_session (7)
    'dc439ea3', '8752c811', '3249768e', '51b23612', 'b759caee',
    'eaca4986', '1de5cff2',
]


def main():
    only = sys.argv[1:] if len(sys.argv) > 1 else None
    data = json.load(open(DATA))
    by_idx = {i: e for i, e in enumerate(data)}
    qidx_by_qid = {}
    for i, e in enumerate(data):
        qidx_by_qid[e['question_id']] = i

    db_dir = "/tmp/cortexm_v065_check"
    os.makedirs(db_dir, exist_ok=True)

    targets = only or FAILS
    n_pass = 0
    for t in targets:
        qidx = None
        q = None
        for e in data:
            if e['question_id'].startswith(t):
                q = e
                qidx = qidx_by_qid[e['question_id']]
                break
        if q is None:
            print(f"[skip] {t}: not found")
            continue
        t0 = time.time()
        # v2: fresh per-question DB — exactly matches the production
        # runner (db_dir/q{qidx}.db + unlink after). The v1 script
        # reused one check.db across questions, which is NOT what the
        # runner does and polluted cross-question state.
        db_path = os.path.join(db_dir, f"q{qidx}.db")
        cfg = _make_config(db_path)
        r = _run_one_question(
            q, qidx, cfg=cfg,
            q_user_id=f"user_{q['question_id']}",
            max_messages_per_q=2000,
            max_seconds_per_q=600,
            include_assistant=True)
        from scripts.longmemeval_canonical_full import _cleanup_db
        _cleanup_db(db_path)
        ok = r['det_correct']
        n_pass += 1 if ok else 0
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {t} ({r['judge_strategy']}, "
              f"{r['n_messages_ingested']} msgs, {time.time()-t0:.0f}s, "
              f"temporal={r.get('temporal_chunks_added', 0)}, "
              f"agg={r.get('agg_chunks_added', 0)}) "
              f"exp='{r['expected_answer'][:40]}'")
    print(f"\n=== {n_pass}/{len(targets)} now PASS ===")


if __name__ == "__main__":
    main()
