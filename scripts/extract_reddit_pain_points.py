#!/usr/bin/env python3
"""Extract pain-point phrases from fetched Reddit thread JSONs.

For each thread:
  - Extract title + visible text from `data.html`.
  - Find sentences that look like user complaints / wishes / feature-requests.
  - Bucket into a coarse category.

Output: top-mentioned phrases & tallied categories across all threads.
"""
from __future__ import annotations
import json, re, glob, html
from collections import Counter, defaultdict
from pathlib import Path

DOWNLOAD_DIR = Path("/home/z/my-project/download")
OUT_FILE = DOWNLOAD_DIR / "reddit_pain_points.json"

WISH_PATTERNS = [
    r"\bI wish\b", r"\bI want\b", r"\bwe need\b", r"\bwhat I really want\b",
    r"\bmissing\b", r"\bwish (it|they|someone)\b", r"\bneeds? to (have|be|support)\b",
    r"\bshould (have|support|be)\b", r"\bdoesn['']?t (support|have|allow)\b",
    r"\bcan['']?t (do|find|use|get)\b", r"\bproblem (is|with)\b", r"\bissue (is|with)\b",
    r"\bfrustrat(?:ing|ed|ion)\b", r"\bpain (point|in the)\b",
    r"\b(no way to|cannot)\b", r"\bhave to (manually|write|build)\b",
    r"\bterrible\b", r"\bawful\b", r"\bsuck(?:s|ed)\b", r"\bbroken\b",
    r"\bblows\b", r"\bgarbage\b", r"\boverengineer(?:ed|ing)\b",
    r"\btoo complex\b", r"\btoo hard\b", r"\btoo complicated\b",
    r"\bjust want\b", r"\bsimple\b", r"\bdead simple\b",
    r"\bwould be (great|nice|amazing|cool)\b", r"\bhopefully\b",
    r"\bI love\b", r"\bgame.changer\b", r"\bnightmare\b",
]
WISH_RE = re.compile("|".join(WISH_PATTERNS), re.IGNORECASE)

CATEGORY_KEYWORDS = {
    "complexity_too_hard": ["complex", "complicated", "overengineer", "too much",
                            "hard to use", "difficult to", "steep learning"],
    "missing_simple_api": ["simple", "dead simple", "minimal", "just want",
                           "one-liner", "easy", "footgun"],
    "vector_recall_poor": ["recall", "missing fact", "doesn't find",
                            "wrong chunk", "bad retrieval", "irrelevant",
                            "no signal", "useless retrieval"],
    "fact_extraction_poor": ["extract", "doesn't extract", "misses",
                              "hallucinat", "wrong fact", "no facts"],
    "provenance_audit": ["provenance", "audit", "trace", "where did",
                         "where this", "source of", "attribution",
                         "cite", "citation", "verifiable"],
    "persistence_versioning": ["version", "rollback", "undo", "snapshot",
                                "revert", "diff", "history", "timeline"],
    "session_replay": ["replay", "fork", "trajectory", "session log",
                       "event stream", "trace view"],
    "ui_dashboard": ["ui", "dashboard", "viewer", "trajectory view",
                     "visualiz", "inspect"],
    "self_host_offline": ["self.host", "local", "offline", "no api",
                          "no cloud", "free tier", "api key"],
    "privacy_security": ["privacy", "pii", "redact", "firewall",
                          "sandbox", "isolat", "rbac", "permission"],
    "time_aware": ["time", "temporal", "bi.temporal", "when was",
                  "valid until", "valid from", "expire"],
    "conflict_resolution": ["conflict", "contradict", "two facts",
                             "out of date", "stale", "supersede"],
    "developer_experience": ["dx", "developer experience", "repl",
                              "creator mode", "interactive", "shell",
                              "playground"],
    "mcp_first": ["mcp", "stdio", "model context protocol", "tool"],
    "vector_vs_keyword": ["bm25", "hybrid", "full.text", "fts",
                           "elasticsearch", "keyword", "sparse"],
}

def html_to_text(h: str) -> str:
    h = re.sub(r"<script[^>]*>.*?</script>", " ", h, flags=re.S|re.I)
    h = re.sub(r"<style[^>]*>.*?</style>", " ", h, flags=re.S|re.I)
    h = re.sub(r"<br\s*/?>", "\n", h, flags=re.I)
    h = re.sub(r"</p>", "\n\n", h, flags=re.I)
    h = re.sub(r"<li[^>]*>", "\n- ", h, flags=re.I)
    h = re.sub(r"<[^>]+>", " ", h)
    h = html.unescape(h)
    h = re.sub(r"[ \t]+", " ", h)
    h = re.sub(r"\n{3,}", "\n\n", h)
    return h.strip()

def sentences(text: str):
    text = re.sub(r"\s+", " ", text)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text)
    return [p.strip() for p in parts if 25 < len(p) < 400]

def main() -> None:
    files = sorted(glob.glob(str(DOWNLOAD_DIR / "page_*.json")))
    all_wishes = []
    per_file_stats = {}
    cat_counter = Counter()
    cat_examples = defaultdict(list)
    raw_phrase_counter = Counter()

    for f in files:
        try:
            j = json.loads(Path(f).read_text())
        except Exception as e:
            print(f"  ! could not parse {f}: {e}")
            continue
        # data may live at j['data'] (z-ai page_reader envelope) or top-level
        data = j.get("data", j) if isinstance(j, dict) else {}
        title = data.get("title", "") or ""
        body = data.get("html", "") or data.get("text", "") or ""
        if not body:
            continue
        text = html_to_text(body)
        sents = sentences(text)
        wishes = [s for s in sents if WISH_RE.search(s)]
        # dedupe near-identical
        seen = set()
        uniq = []
        for s in wishes:
            key = re.sub(r"\W+", " ", s.lower())[:120]
            if key in seen: continue
            seen.add(key)
            uniq.append(s)
        per_file_stats[Path(f).name] = {
            "title": title[:140],
            "wishes": len(uniq),
            "html_bytes": len(body),
        }
        for s in uniq:
            all_wishes.append({"file": Path(f).name, "title": title[:80], "wish": s})
            # categorize
            sl = s.lower()
            for cat, kws in CATEGORY_KEYWORDS.items():
                if any(kw in sl for kw in kws):
                    cat_counter[cat] += 1
                    if len(cat_examples[cat]) < 8:
                        cat_examples[cat].append(s)
                    # raw keyword hits for ≥10-mention threshold
                    for kw in kws:
                        if kw in sl:
                            raw_phrase_counter[kw] += 1
        # also tally raw keyword hits across the WHOLE body (not just wish sentences)
        low = text.lower()
        for cat, kws in CATEGORY_KEYWORDS.items():
            for kw in kws:
                c = low.count(kw)
                if c:
                    raw_phrase_counter[kw] += c

    # Compute "≥10 mentions" pain points
    pain_points_10plus = [
        {"keyword": k, "mentions": v}
        for k, v in raw_phrase_counter.most_common()
        if v >= 10
    ][:40]

    OUT_FILE.write_text(json.dumps({
        "n_threads_parsed": len(per_file_stats),
        "per_thread": per_file_stats,
        "category_counts": dict(cat_counter.most_common()),
        "category_examples": {k: v for k, v in cat_examples.items() if v},
        "pain_points_10plus_mentions": pain_points_10plus,
        "all_wishes": all_wishes[:200],
    }, indent=2)[:200_000])  # cap at 200KB
    print(f"  wrote {OUT_FILE} ({OUT_FILE.stat().st_size} bytes)")
    print(f"  threads parsed: {len(per_file_stats)}")
    print(f"  total wish sentences: {len(all_wishes)}")
    print(f"  top categories:")
    for c, n in cat_counter.most_common(8):
        print(f"    {c:30s} {n}")
    print(f"  pain points (>=10 mentions):")
    for p in pain_points_10plus[:15]:
        print(f"    {p['keyword']:25s} {p['mentions']}")

if __name__ == "__main__":
    main()
