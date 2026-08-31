"""Diagnose LoCoMo failures: retrieval miss vs judge gap vs derivation.

For every failed question in the comparable subset (single/multi/temporal
plus open_domain), checks:
  1. gold-in-corpus  — does the gold answer appear in the FULL
     conversation text? (all turns joined)
  2. gold-in-context — does it appear in the retrieved context_block?
  3. gold-in-timeline — is it a session date visible in the timeline?

Classifies:
  RETRIEVAL_MISS : in corpus, not in context (search failed to surface)
  JUDGE_MISS     : in context, judged wrong (format/matching gap)
  DERIVATION     : not in corpus verbatim (needs inference/arithmetic)
"""
import json
import re
import sys
from collections import Counter

RESULTS = "benchmarks/results/locomo/locomo_full_k60.json"
DATA = "data/locomo/locomo10.json"

data = json.load(open(DATA))
by_cid = {str(c.get("sample_id")): c for c in data}
out = json.load(open(RESULTS))

_norm_re = re.compile(r"[^a-z0-9]+")


def norm(s: str) -> str:
    return _norm_re.sub(" ", (s or "").lower()).strip()


def corpus_text(c: dict) -> str:
    parts = []
    for k, v in c["conversation"].items():
        if k.startswith("session_") and not k.endswith("_date_time"):
            for m in v:
                parts.append(m.get("text") or "")
                if m.get("blip_caption"):
                    parts.append(m["blip_caption"])
    return norm(" \n".join(parts))


cls_counts: Counter = Counter()
cls_by_cat: dict[str, Counter] = {}
examples: dict[str, list] = {}
strategies: Counter = Counter()

for conv in out["results"]:
    cid = conv["conversation_id"]
    c = by_cid.get(cid)
    if c is None:
        continue
    ct = corpus_text(c)
    for r in conv["results"]:
        if r["det_correct"] or not r["gold"]:
            continue
        cat = r["category"]
        if cat == "adversarial":
            continue
        g = norm(r["gold"])
        cb = norm(r["context_block_preview"])  # preview only (1500 chars)
        # token-level presence (looser than substring)
        g_toks = [t for t in g.split() if len(t) > 2]
        in_corpus = all(t in ct for t in g_toks) if g_toks else False
        in_cb = g in cb or all(t in cb for t in g_toks) if g_toks else False
        # substring (strict)
        strict_corpus = g in ct
        strict_cb = g in cb

        if strict_cb or in_cb:
            cls = "JUDGE_MISS"
        elif strict_corpus or in_corpus:
            cls = "RETRIEVAL_MISS"
        else:
            cls = "DERIVATION"
        cls_counts[cls] += 1
        cls_by_cat.setdefault(cat, Counter())[cls] += 1
        strategies[r["judge_strategy"]] += 1
        examples.setdefault(cls, []).append({
            "cid": cid, "cat": cat, "q": r["question"][:90],
            "gold": r["gold"][:80], "strategy": r["judge_strategy"],
            "strict_cb": strict_cb, "strict_corpus": strict_corpus,
        })

print("== failure classes (comparable + open_domain, n=%d) ==" %
      sum(cls_counts.values()))
for k, v in cls_counts.most_common():
    print(f"  {k:<15} {v}")
print("\n== by category ==")
for cat, cc in sorted(cls_by_cat.items()):
    print(f"  {cat:<12} " + "  ".join(f"{k}={v}" for k, v in cc.most_common()))
print("\n== failed-question judge strategies ==")
for k, v in strategies.most_common():
    print(f"  {k:<20} {v}")

print("\n== JUDGE_MISS examples (10) ==")
for e in examples.get("JUDGE_MISS", [])[:10]:
    print(f"  [{e['cid']}|{e['cat']}|{e['strategy']}] {e['q']}")
    print(f"    gold: {e['gold']}")
print("\n== RETRIEVAL_MISS examples (10) ==")
for e in examples.get("RETRIEVAL_MISS", [])[:10]:
    print(f"  [{e['cid']}|{e['cat']}|{e['strategy']}] {e['q']}")
    print(f"    gold: {e['gold']}")
print("\n== DERIVATION examples (10) ==")
for e in examples.get("DERIVATION", [])[:10]:
    print(f"  [{e['cid']}|{e['cat']}|{e['strategy']}] {e['q']}")
    print(f"    gold: {e['gold']}")
