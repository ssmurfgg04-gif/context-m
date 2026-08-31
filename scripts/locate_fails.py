"""Locate all failing answers in the raw haystacks (fast, no retrieval)."""
import json
import re
import sys

sys.path.insert(0, '/home/z/my-project')
from scripts.diag_failures import find_answer_locations

DATA = '/home/z/my-project/benchmarks/data/longmemeval/longmemeval_s_cleaned.json'


def main():
    data = json.load(open(DATA))
    byid = {e['question_id']: e for e in data}
    fails = ['gpt4_f2262a51', '1f2b8d4f', 'e25c3b8d', 'bb7c3b45', '60159905',
             'ef9cf60a', 'd6062bb9', 'gpt4_e061b84g', 'gpt4_d6585ce9',
             '0bc8ad93', 'gpt4_8279ba03', 'cc6d1ec1', 'd01c6aa8', '0977f2af',
             'dc439ea3', '8752c811', '3249768e', '51b23612', 'b759caee',
             'eaca4986', '1de5cff2']
    for qid in fails:
        q = byid.get(qid)
        if q is None:
            print(f'{qid}: NOT FOUND')
            continue
        hits = find_answer_locations(q, q['answer'])
        if not hits:
            print(f'{qid}: NEEDS-DERIVATION (answer text absent from haystack)')
            continue
        for h in hits[:3]:
            trunc = ('ASSIST-TRUNC-800' if (h['role'] == 'assistant' and h['pos'] > 800)
                     else 'ok')
            print(f"{qid}: sess#{h['session_idx']}/{len(q['haystack_sessions'])} "
                  f"msg#{h['msg_idx']} ({h['role']}) pos={h['pos']}/{h['len']} [{trunc}]")
        print(f"    around: ...{hits[0]['around'][:180]}...")


if __name__ == '__main__':
    main()
