"""Deep-dive: quantify fixable failure sub-classes.

1. DERIVATION/temporal: how many golds are bare years / dates, and do
   relative-time phrases ("last year", "N years ago") appear near the
   topic chunks?
2. RETRIEVAL_MISS: at what rank does the gold-bearing chunk sit in a
   raw verbatim scan (does a speaker-scope or deeper-k pass reach it)?
3. Adversarial rubric note distribution.
"""
import json
import re
import sys
from collections import Counter

sys.path.insert(0, "/home/z/my-project")

RESULTS = "benchmarks/results/locomo/locomo_full.json"
DATA = "data/locomo/locomo10.json"

data = json.load(open(DATA))
by_cid = {str(c.get("sample_id")): c for c in data}
out = json.load(open(RESULTS))

_norm_re = re.compile(r"[^a-z0-9]+")


def norm(s):
    return _norm_re.sub(" ", (s or "").lower()).strip()


# ---- 1. DERIVATION gold shapes ----
year_golds = Counter()
deri = []
for conv in out["results"]:
    cid = conv["conversation_id"]
    for r in conv["results"]:
        if r["det_correct"] or not r["gold"] or r["category"] == "adversarial":
            continue
        g = r["gold"]
        if re.fullmatch(r"\d{4}", g.strip()):
            year_golds[("bare_year", r["category"])] += 1
        elif re.search(r"\d{4}", g):
            year_golds[("has_year", r["category"])] += 1
        elif re.match(r"^(yes|no)\b", g, re.I):
            year_golds[("bool", r["category"])] += 1

print("== gold shapes among ALL wrong (incl. retrieval misses) ==")
for k, v in sorted(year_golds.items()):
    print(f"  {k}: {v}")

# ---- 2. relative-time phrases in corpus near gold years ----
_REL_RE = re.compile(
    r"\b(?:last\s+year|next\s+year|previous\s+year|past\s+year|"
    r"(?:a\s+|one\s+|two\s+|three\s+|four\s+|five\s+|couple\s+of\s+)?"
    r"years?\s+ago|last\s+month|months?\s+ago|last\s+week|weeks?\s+ago|"
    r"yesterday|today|tomorrow)\b", re.I)

n_rel = 0
n_topic_rel = 0
samples = []
for conv in out["results"]:
    cid = conv["conversation_id"]
    c = by_cid[cid]
    convtext = []
    sess_keys = sorted([k for k in c["conversation"]
                        if re.fullmatch(r"session_\d+", k)],
                       key=lambda k: int(k.split("_")[1]))
    for sk in sess_keys:
        for m in c["conversation"][sk]:
            convtext.append(((m.get("text") or ""),
                             m.get("speaker") or "", sk))
    n_rel_corpus = sum(1 for t, _, _ in convtext if _REL_RE.search(t))
    n_rel += n_rel_corpus
    for r in conv["results"]:
        if r["det_correct"] or not r["gold"] or r["category"] not in (
                "temporal", "multi_hop", "single_hop"):
            continue
        if not re.search(r"\bwhen\b|\bhow long\b|\bwhat year\b", 
                         r["question"], re.I):
            continue
        # topic tokens from question
        qt = set(norm(r["question"]).split()) - {"when", "did", "the",
                                                 "a", "an", "do", "does",
                                                 "what", "how", "long",
                                                 "year", "years"}
        for text, spk, sk in convtext:
            if _REL_RE.search(text) and qt & set(norm(text).split()):
                n_topic_rel += 1
                if len(samples) < 12:
                    samples.append((cid, r["question"][:70], r["gold"],
                                    text[:110]))
                break

print(f"\nrelative-time phrases in corpus: {n_rel}")
print(f"'when'-style failed questions whose topic chunks carry a "
      f"relative phrase: {n_topic_rel}")
for s in samples:
    print(f"  [{s[0]}] {s[1]} -> {s[2]!r}")
    print(f"      chunk: {s[3]}")

# ---- 3. adversarial notes ----
notes = Counter()
for conv in out["results"]:
    for r in conv["results"]:
        if r["adversarial_correct"] is not None:
            notes[r["adversarial_note"]] += 1
print("\n== adversarial rubric notes ==")
for k, v in notes.most_common():
    print(f"  {k}: {v}")

# ---- 4. for RETRIEVAL_MISS: is the gold in the OTHER speaker's turns? ----
speaker_issue = 0
other_speaker_gold = 0
for conv in out["results"]:
    cid = conv["conversation_id"]
    c = by_cid[cid]
    speakers = [c["conversation"].get("speaker_a"),
                c["conversation"].get("speaker_b")]
    turns_by_speaker = {}
    for k, v in c["conversation"].items():
        if k.startswith("session_") and not k.endswith("_date_time"):
            for m in v:
                spk = m.get("speaker") or "?"
                turns_by_speaker.setdefault(spk, []).append(
                    (m.get("text") or "") + " " + (m.get("blip_caption") or ""))
    for r in conv["results"]:
        if r["det_correct"] or not r["gold"] or r["category"] == "adversarial":
            continue
        g = norm(r["gold"])
        toks = [t for t in g.split() if len(t) > 2]
        if not toks:
            continue
        in_any = all(t in norm(" ".join(turns_by_speaker.get(s, [])))
                     for s in speakers) if speakers else False
        # which speaker's turns contain ALL gold tokens?
        holders = [s for s, texts in turns_by_speaker.items()
                   if all(t in norm(" ".join(texts)) for t in toks)]
        q = r["question"].lower()
        asked = [s for s in speakers if s and s.lower() in q]
        if holders and asked and not (set(holders) & set(asked)):
            other_speaker_gold += 1
print(f"\nRETRIEVAL-style failures where gold lives ONLY in the "
      f"non-asked speaker's turns: {other_speaker_gold}")
