"""Re-run the 8 failing canonical questions from the 154-Q sample
to verify v0.5.5 fixes (SUM/DIFF judge + holiday-date + aggregation
retrieval). Writes results to /tmp/v055_recheck.json.

Uses the per-question Memory runner directly. Each Q gets ~180s ingest
cap to keep total runtime bounded.

Usage:
    python scripts/v055_recheck_failures.py
"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cortexm.api.memory import Memory
from scripts.longmemeval_canonical_full import (
    _make_config, _run_one_question, _cleanup_db,
    _is_aggregation_question, _extract_topic_keywords,
)
from scripts.longmemeval_canonical import (
    DEFAULT_DATA_PATH, _flatten_haystack, SUBTASK_MAP,
    _infer_entity_attribute,
)
from scripts.longmemeval_judge import det_judge, LongMemEvalQuestion

FAIL_INDICES = [8, 32, 76, 97, 107, 111, 115, 119]

def main():
    print("=" * 70)
    print(" v0.5.5 fix verification — re-running 8 failing questions")
    print("=" * 70)
    print()

    with open(DEFAULT_DATA_PATH) as f:
        data = json.load(f)

    print("[sanity] Aggregation question detection:")
    for idx in FAIL_INDICES:
        q = data[idx]
        is_agg = _is_aggregation_question(q['question'])
        topics = _extract_topic_keywords(q['question'])
        marker = " [AGG]" if is_agg else ""
        print(f"  #{idx}: {q['question'][:90]}{marker}")
        if topics:
            print(f"        topics: {topics}")
    print()

    db_dir = "/tmp/cortexm_v055_recheck"
    os.makedirs(db_dir, exist_ok=True)
    results = []
    for idx in FAIL_INDICES:
        q = data[idx]
        qid = q.get('question_id', f'q{idx}')
        question = q['question']
        answer = str(q.get('answer', ''))
        qtype = q['question_type']
        subtask = SUBTASK_MAP.get(qtype, 'single_session')

        print(f"\n[#{idx}] [{subtask}] {qid}")
        print(f"  Q: {question[:100]}")
        print(f"  A: {answer}")

        db_path = os.path.join(db_dir, f"q{idx}.db")
        cfg = _make_config(db_path)
        q_user_id = f"user_{qid}"
        t_start = time.time()
        try:
            r = _run_one_question(
                q, idx, cfg=cfg, q_user_id=q_user_id,
                max_messages_per_q=1500,
                max_seconds_per_q=180,
                include_assistant=True)
        except Exception as e:
            r = {
                "qid": qid, "global_idx": idx,
                "question_type": qtype, "subtask": subtask,
                "question": question, "expected_answer": answer,
                "n_messages_ingested": 0,
                "ingest_s": 0.0, "retrieve_s": 0.0,
                "judge_strategy": "error",
                "context_block_preview": "",
                "det_correct": False,
                "error": f"unhandled: {e}",
            }
            print(f"  [ERROR] {e}")
        elapsed = time.time() - t_start
        flag = "✓" if r.get('det_correct') else "✗"
        print(f"  [{flag}] strategy={r.get('judge_strategy', '?')} "
              f"elapsed={elapsed:.1f}s "
              f"vh={r.get('verbatim_hits', 0)}")
        results.append(r)
        _cleanup_db(db_path)

    # Summarize
    n_correct = sum(1 for r in results if r.get('det_correct'))
    print("\n" + "=" * 70)
    print(f" {n_correct}/{len(results)} fixed")
    print("=" * 70)
    for r in results:
        flag = "✓" if r.get('det_correct') else "✗"
        print(f"  [{flag}] #{r['global_idx']} [{r.get('judge_strategy','?')}] "
              f"answer={r.get('expected_answer','')[:50]}")

    out_path = "/home/z/my-project/benchmarks/results/v055_recheck_failures.json"
    with open(out_path, 'w') as f:
        json.dump({
            "n_correct": n_correct,
            "n_total": len(results),
            "results": results,
        }, f, indent=2)
    print(f"\nResults saved to {out_path}")

if __name__ == "__main__":
    main()
