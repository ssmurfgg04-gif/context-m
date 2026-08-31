"""Inspect the official LoCoMo dataset structure (locomo10.json)."""
import json
from collections import Counter

data = json.load(open('data/locomo/locomo10.json'))
print('top-level type:', type(data).__name__)
if isinstance(data, dict):
    print('conv keys:', list(data.keys()))
    items = list(data.items())
else:
    items = list(enumerate(data))

cid, c = items[0]
print('conv', repr(cid), 'keys:', sorted(c.keys()))
conv = c['conversation']
print('conversation field type:', type(conv).__name__)
if isinstance(conv, dict):
    skeys = list(conv.keys())
    print('session keys (first 5):', skeys[:5], '... total', len(skeys))
    s = conv[skeys[0]]
else:
    s = conv[0]
print('session keys:', sorted(s.keys()))
print('session date_str:', s.get('date_str'))
dlg = s.get('dialogue')
print('dialogue type:', type(dlg).__name__)
if isinstance(dlg, dict):
    dkeys = list(dlg.keys())
    print('dialogue keys (first 5):', dkeys[:5], 'total', len(dkeys))
    d = dlg[dkeys[0]]
else:
    d = dlg[0]
print('msg:', json.dumps(d)[:400])

# Aggregate stats across all conversations
cat_counter = Counter()
n_qa = 0
n_sessions = 0
n_turns = 0
qa_keys = Counter()
answer_types = Counter()
for cid, c in items:
    conv = c['conversation']
    sessions = conv.values() if isinstance(conv, dict) else conv
    for s in sessions:
        n_sessions += 1
        dlg = s.get('dialogue')
        if isinstance(dlg, dict):
            n_turns += len(dlg)
        elif isinstance(dlg, list):
            n_turns += len(dlg)
    for qa in c.get('qa', []):
        n_qa += 1
        cat_counter[qa.get('category')] += 1
        qa_keys.update(qa.keys())
        answer_types[type(qa.get('answer')).__name__] += 1

print()
print('TOTAL: convs=%d sessions=%d turns=%d qa=%d' % (len(items), n_sessions, n_turns, n_qa))
print('QA category distribution:', dict(sorted(cat_counter.items(), key=lambda kv: str(kv[0]))))
print('QA keys:', dict(qa_keys))
print('answer types:', dict(answer_types))

# Sample QAs per category
by_cat = {}
for cid, c in items:
    for qa in c.get('qa', []):
        by_cat.setdefault(qa.get('category'), []).append(qa)
for cat in sorted(by_cat, key=str):
    print()
    print('=== category', cat, '- %d questions ===' % len(by_cat[cat]))
    for qa in by_cat[cat][:3]:
        print('  Q:', qa.get('question'))
        print('  A:', repr(qa.get('answer')))
        print('  evidence:', qa.get('evidence'))
