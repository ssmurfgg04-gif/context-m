#!/usr/bin/env python3
"""Aggregate search-result snippets across targeted queries.

For each query file in download/q_*.json:
  - Load the snippet list.
  - Append all `name` + `snippet` text to a per-topic bucket.
  - Tokenize to sentences.

Output: per-topic top-mentioned keywords + total mentions across topics.
Pain points with >=10 mentions across all sources are reported.
"""
from __future__ import annotations
import json, re, glob, html
from collections import Counter, defaultdict
from pathlib import Path

DOWNLOAD = Path("/home/z/my-project/download")

# Topic key derived from filename (q_<topic>.json)
TOPIC_KEYWORDS = {
    "simple":       ["simple", "dead simple", "just works", "minimal", "easy", "footgun", "one-liner", "straightforward"],
    "complex":      ["complex", "complicated", "overengineer", "too much", "hard to use", "difficult", "steep learning", "bloated"],
    "hybrid":       ["hybrid", "bm25", "keyword", "sparse", "full-text", "fts", "elasticsearch", "reranker", "rerank"],
    "local":        ["self-host", "self host", "local-first", "local first", "offline", "no api", "no cloud", "free tier", "on-prem"],
    "prov":         ["provenance", "audit", "trace", "where did", "source of", "attribution", "citation", "cite", "verifiable", "provenance"],
    "dx":           ["dx", "developer experience", "repl", "playground", "creator mode", "shell", "interactive"],
    "time":         ["temporal", "bi-temporal", "bi temporal", "when was", "valid from", "valid until", "expire", "version", "rollback", "snapshot", "diff", "history", "timeline"],
    "extract":      ["extract", "hallucinat", "wrong fact", "misses", "wrong chunk", "irrelevant", "no signal", "useless retrieval", "bad retrieval"],
    "mcp":          ["mcp", "model context protocol", "stdio", "tool"],
    "ui":           ["ui", "dashboard", "viewer", "trajectory view", "visualiz", "inspect", "gui"],
    "privacy":      ["privacy", "pii", "redact", "firewall", "sandbox", "isolat", "rbac", "permission"],
    "conflict":     ["conflict", "contradict", "supersede", "stale", "out of date", "out-of-date"],
}

def snippets(j):
    """Return list of dicts with name/snippet/host_name."""
    if isinstance(j, list):
        return j
    if isinstance(j, dict):
        for k in ("results","data","items"):
            if k in j and isinstance(j[k], list):
                return j[k]
    return []

def sentence_split(text):
    text = re.sub(r"\s+", " ", text)
    return [p.strip() for p in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text) if 25 < len(p) < 400]

def main() -> None:
    files = sorted(glob.glob(str(DOWNLOAD / "q_*.json")))
    print(f"  query files found: {len(files)}")
    keyword_mentions = Counter()
    topic_keyword_mentions = defaultdict(Counter)
    all_sentences = []
    for f in files:
        topic = Path(f).stem[2:]  # strip q_
        try:
            data = snippets(json.loads(Path(f).read_text()))
        except Exception as e:
            print(f"  ! {f}: {e}")
            continue
        # bucket all text
        text_blocks = []
        for r in data:
            if not isinstance(r, dict): continue
            name = r.get("name","") or ""
            snip = r.get("snippet","") or ""
            text_blocks.append(f"{name}. {snip}")
        for tb in text_blocks:
            for s in sentence_split(tb):
                all_sentences.append({"topic": topic, "sentence": s})
        # count keyword hits
        all_text_lower = " ".join(text_blocks).lower()
        if topic in TOPIC_KEYWORDS:
            for kw in TOPIC_KEYWORDS[topic]:
                c = all_text_lower.count(kw.lower())
                if c:
                    topic_keyword_mentions[topic][kw] += c
                    keyword_mentions[kw] += c
        # cross-topic keyword hits
        for tname, kws in TOPIC_KEYWORDS.items():
            for kw in kws:
                c = all_text_lower.count(kw.lower())
                if c:
                    keyword_mentions[kw] += c
    # report
    print(f"  total sentences: {len(all_sentences)}")
    print(f"  total unique keywords: {len(keyword_mentions)}")
    print("\n=== Pain-point keywords with ≥10 mentions (across all queries) ===")
    top = [(k,v) for k,v in keyword_mentions.most_common() if v >= 10]
    for k,v in top[:40]:
        print(f"  {v:4d}  {k}")
    print("\n=== Per-topic top keywords ===")
    for t, ctr in topic_keyword_mentions.items():
        if not ctr: continue
        print(f"  [{t}]")
        for k,v in ctr.most_common(5):
            print(f"      {v:4d}  {k}")
    # write out
    out = {
        "n_query_files": len(files),
        "n_total_sentences": len(all_sentences),
        "pain_points_10plus": [{"keyword":k,"mentions":v} for k,v in top],
        "per_topic_keywords": {t: dict(c.most_common()) for t,c in topic_keyword_mentions.items() if c},
        "sample_sentences": [s["sentence"] for s in all_sentences[:60]],
    }
    Path(DOWNLOAD / "reddit_pain_points.json").write_text(json.dumps(out, indent=2)[:200_000])
    print(f"\n  wrote {DOWNLOAD / 'reddit_pain_points.json'}")

if __name__ == "__main__":
    main()
