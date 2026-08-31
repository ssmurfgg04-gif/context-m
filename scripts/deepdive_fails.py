"""Deep-dive specific failing questions: dump answer + relevant haystack text."""
import json
import re
import sys

DATA = '/home/z/my-project/benchmarks/data/longmemeval/longmemeval_s_cleaned.json'


def show(qid_prefix, needles, ctx=260, max_hits=12):
    data = json.load(open(DATA))
    q = next(e for e in data if e['question_id'].startswith(qid_prefix))
    print('=' * 90)
    print(f"QID {q['question_id']} | question_date: {q.get('question_date')}")
    print(f"Q: {q['question']}")
    print(f"A: {q['answer']}")
    print(f"answer_session_ids: {q.get('answer_session_ids')}")
    dates = q.get('haystack_dates', [])
    sids = q.get('haystack_session_ids', [])
    hits = 0
    for si, session in enumerate(q['haystack_sessions']):
        for mi, msg in enumerate(session):
            content = msg.get('content', '') or ''
            lc = content.lower()
            for nd in needles:
                if nd in lc:
                    pos = lc.find(nd)
                    print(f"\n--- sess#{si} ({sids[si] if si < len(sids) else '?'}) "
                          f"[{dates[si] if si < len(dates) else '?'}] "
                          f"msg#{mi} ({msg['role']}) nd='{nd}' pos={pos}/{len(content)}:")
                    print('   ...' + content[max(0, pos - ctx):pos + ctx].replace('\n', ' ') + '...')
                    hits += 1
                    break
        if hits >= max_hits:
            break
    if hits == 0:
        print(f"\n(no haystack match for needles {needles})")


if __name__ == '__main__':
    qid = sys.argv[1]
    needles = sys.argv[2].split('|')
    show(qid, needles)
