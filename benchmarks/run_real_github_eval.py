#!/usr/bin/env python3
"""Real-GitHub evaluation — the zero-LLM-vs-LLM extractor comparison the
reviewer demanded, on REAL human text.

Stages:
  1. load    real issue threads (benchmarks/fetch_real_github.py output)
  2. extract μ=0 pattern extractor over every comment (timed)
             LLM reference extractor over the same comments (timed, costed)
  3. score   recall of μ=0 w.r.t. the LLM reference; precision via reverse
             matching; per-relation breakdown; cost & latency table
  4. qa      LLM-generated Q/A over each thread -> Context-M ingest ->
             retrieval -> canonical LLM judge
  5. write   benchmarks/results/real_github/*.json

Usage:
  python benchmarks/run_real_github_eval.py [--threads N] [--no-qa]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from context_m import metrics  # noqa: E402
from context_m.api.memory import Memory  # noqa: E402
from context_m.bench.ood import T0  # noqa: E402
from context_m.bridge.extractor import Extractor  # noqa: E402
from context_m.bridge.patterns import ExtractionContext  # noqa: E402
from context_m.config import Config  # noqa: E402
from context_m.util import normalize  # noqa: E402

LLM_DIR = REPO / "benchmarks" / "llm"
RG_DIR = REPO / "benchmarks" / "real_github"
RESULTS = REPO / "benchmarks" / "results" / "real_github"


def sh(cmd: list[str]) -> None:
    print(f"+ {' '.join(str(c) for c in cmd)}", flush=True)
    r = subprocess.run(cmd, cwd=str(REPO))
    if r.returncode != 0:
        raise SystemExit(f"command failed: {cmd}")


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text().split("\n") if l.strip()]


def read_jsonl_opt(p: Path) -> list[dict]:
    """Like read_jsonl but tolerant of a missing/empty LLM-stage output
    (quota exhaustion upstream must degrade, not crash the pipeline)."""
    if not p.exists() or not p.stat().st_size:
        return []
    return read_jsonl(p)


def write_jsonl(p: Path, rows: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _words(s: str) -> set[str]:
    return set(normalize(s).split())


def _match(a: str, b: str) -> bool:
    """Fuzzy value match: containment or >=0.6 content-word overlap."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / max(len(wa), len(wb)) >= 0.6


def extraction_comparison(threads: list[dict]) -> dict:
    cfg = Config()
    extractor = Extractor(cfg)

    # ---- μ=0 pass (timed) ------------------------------------------------
    t0 = time.perf_counter()
    u0_facts: list[dict] = []
    comments: list[dict] = []
    for th in threads:
        subject = th["author"]
        for c in th["comments"]:
            comments.append({"id": f"{c['author']}@{th['id']}",
                             "text": c["body"],
                             "subject": c["author"],
                             "author": c["author"]})
        # extraction layer only — plain text, author as subject hint
        for c in th["comments"]:
            ctx = ExtractionContext(user_id=th["id"], ts=T0,
                                    speaker="user",
                                    subject_name=c["author"],
                                    lexicon=set())
            try:
                cands = extractor.extract(c["body"], ctx)
            except Exception:
                cands = []
            for cd in cands:
                if cd.pattern == "mention_fallback" or cd.confidence < 0.3:
                    continue
                u0_facts.append({"comment": f"{c['author']}@{th['id']}",
                                 "subject": cd.subject,
                                 "relation": cd.relation,
                                 "value": cd.value})
    u0_seconds = time.perf_counter() - t0

    # ---- LLM reference pass ----------------------------------------------
    items_path = RG_DIR / "comments.jsonl"
    write_jsonl(items_path, comments)
    ref_path = RG_DIR / "reference_facts.jsonl"
    t1 = time.perf_counter()
    sh(["node", str(LLM_DIR / "extract_facts.mjs"), str(items_path),
        str(ref_path)])
    llm_seconds = time.perf_counter() - t1
    ref_rows = read_jsonl(ref_path)
    llm_facts: list[dict] = []
    tokens = 0
    for r in ref_rows:
        tokens += (r.get("usage") or {}).get("total_tokens") or 0
        for f in r.get("facts", []):
            llm_facts.append({"comment": r["id"],
                              "subject": f.get("subject", ""),
                              "relation": f.get("relation", ""),
                              "value": f.get("value", "")})

    # ---- scoring -----------------------------------------------------------
    def comment_key(x): return x["comment"]

    u0_by_c: dict[str, list] = {}
    for f in u0_facts:
        u0_by_c.setdefault(comment_key(f), []).append(f)
    llm_by_c: dict[str, list] = {}
    for f in llm_facts:
        llm_by_c.setdefault(comment_key(f), []).append(f)

    recall_hit = recall_tot = 0
    for cid, refs in llm_by_c.items():
        got = u0_by_c.get(cid, [])
        for ref in refs:
            recall_tot += 1
            if any(_match(ref["value"], g["value"]) or
                   _match(ref["value"], g["subject"]) for g in got):
                recall_hit += 1
    prec_hit = prec_tot = 0
    for cid, got in u0_by_c.items():
        refs = llm_by_c.get(cid, [])
        for g in got:
            prec_tot += 1
            if any(_match(g["value"], ref["value"]) or
                   _match(g["value"], ref["subject"]) for ref in refs):
                prec_hit += 1

    rel_stats: dict[str, list[int]] = {}
    for cid, refs in llm_by_c.items():
        got = u0_by_c.get(cid, [])
        for ref in refs:
            k = ref["relation"][:24]
            rel_stats.setdefault(k, [0, 0])
            rel_stats[k][1] += 1
            if any(_match(ref["value"], g["value"]) for g in got):
                rel_stats[k][0] += 1

    n_comments = len(comments)
    return {
        "n_threads": len(threads),
        "n_comments": n_comments,
        "u0": {"facts": len(u0_facts), "seconds": round(u0_seconds, 3),
               "ms_per_comment": round(u0_seconds * 1000 / max(n_comments, 1), 2),
               "cost_usd": 0.0},
        "llm_reference": {"facts": len(llm_facts),
                          "seconds": round(llm_seconds, 1),
                          "ms_per_comment": round(
                              llm_seconds * 1000 / max(n_comments, 1), 2),
                          "tokens": tokens,
                          "model": ref_rows[0].get("model") if ref_rows else None},
        "recall_vs_llm_reference": round(recall_hit / max(recall_tot, 1), 4),
        "precision_vs_llm_reference": round(prec_hit / max(prec_tot, 1), 4),
        "per_relation_recall": {k: round(v[0] / v[1], 4)
                                for k, v in sorted(rel_stats.items(),
                                                   key=lambda kv: -kv[1][1])
                                [:12]},
        "interpretation": (
            "recall = share of LLM-reference facts the μ=0 extractor also "
            "produced on the same real comment; precision = share of μ=0 "
            "facts the LLM reference corroborates. The LLM reference is a "
            "strong-but-imperfect yardstick, not gold truth."),
    }


def qa_eval(threads: list[dict]) -> dict:
    # ---- generate questions ------------------------------------------------
    threads_path = RG_DIR / "threads.jsonl"
    qa_path = RG_DIR / "qa_pairs.jsonl"
    sh(["node", str(LLM_DIR / "qa_generate.mjs"), str(threads_path),
        str(qa_path)])
    qa = {row["id"]: row for row in read_jsonl_opt(qa_path)}
    if not qa:
        print("WARNING: qa_generate produced no pairs (LLM quota exhausted?) "
              "— writing degraded qa_eval.json instead of crashing")
        return {
            "n_questions": 0,
            "degraded": True,
            "reason": "LLM quota exhausted before QA generation; rerun the "
                      "workflow later (the judge cache resumes, only the "
                      "missing items are re-billed)",
            "judge_models": [],
            "interpretation": "Degraded run — no numbers, by design. A "
                              "self-crashing pipeline would hide the quota "
                              "problem behind a red X; this keeps the "
                              "extraction comparison usable.",
        }

    # ---- ingest threads into the fabric ------------------------------------
    cfg = Config()
    cfg.apply_rules_each_add = False
    m = Memory(cfg)
    metrics.reset_counters()
    for th in threads:
        msgs = [{"role": "user",
                 "content": f"[{c['author']}] {c['body'][:2000]}",
                 "timestamp": c["created_at"]}
                for c in th["comments"]]
        if th.get("body"):
            msgs.insert(0, {"role": "user",
                            "content": f"[{th['author']}] {th['body'][:2000]}",
                            "timestamp": th["created_at"]})
        m.add(msgs, user_id=th["id"],
              timestamp=_parse(th["created_at"]))
    m.apply_rules()

    # ---- retrieve + judge ----------------------------------------------------
    judge_items: list[dict] = []
    for th in threads:
        qas = qa.get(th["id"], {}).get("questions", [])
        for qi, q in enumerate(qas[:5]):
            out = m.search(q["question"], user_id=th["id"], k=10)
            judge_items.append({
                "id": f"{th['id']}-q{qi}",
                "ability": "AB" if q["answer"] == "NOT_IN_THREAD" else "IE",
                "question": q["question"],
                "expected": [
                    f"Gold answer (from thread): {q['answer']}",
                    f"Supporting quote: {q.get('quote', '')}"],
                "context": out["context_block"],
                "gold": q["answer"],
            })
    items_path = RG_DIR / "qa_judge_items.jsonl"
    write_jsonl(items_path, judge_items)
    scored_path = RG_DIR / "qa_judge_scored.jsonl"
    sh(["node", str(LLM_DIR / "judge_llm.mjs"), str(items_path),
        str(scored_path)])
    scored = {r["id"]: r for r in read_jsonl_opt(scored_path)}

    n = vals = 0
    per_kind = {"answerable": [0, 0], "abstention": [0, 0]}
    for it in judge_items:
        s = scored.get(it["id"], {}).get("score")
        if s is None:
            continue
        n += 1
        vals += s
        kind = "abstention" if it["ability"] == "AB" else "answerable"
        per_kind[kind][0] += s
        per_kind[kind][1] += 1
    m.close()
    if n == 0:
        return {
            "n_questions": 0,
            "degraded": True,
            "reason": "LLM judge scored 0 items (quota exhausted?) — rerun "
                      "later; the judge resumes from cache",
            "judge_models": [],
            "interpretation": "Degraded run — no numbers reported.",
        }
    return {
        "n_questions": n,
        "llm_judge_overall": round(vals / max(n, 1), 4),
        "answerable": round(per_kind["answerable"][0]
                            / max(per_kind["answerable"][1], 1), 4),
        "abstention": round(per_kind["abstention"][0]
                            / max(per_kind["abstention"][1], 1), 4),
        "judge_models": sorted({r.get("judge_model") for r in
                                scored.values() if r.get("judge_model")}),
        "interpretation": (
            "Context-M retrieval over REAL GitHub threads, graded by the "
            "BEAM-style LLM judge. Judge model identity recorded; canonical "
            "BEAM uses gpt-5 — not directly comparable."),
    }


def _parse(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=9)
    ap.add_argument("--no-qa", action="store_true")
    args = ap.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)
    threads = read_jsonl(RG_DIR / "threads.jsonl")[:args.threads]
    print(f"loaded {len(threads)} real threads")

    comp = extraction_comparison(threads)
    (RESULTS / "extraction_comparison.json").write_text(
        json.dumps(comp, indent=1))
    print("extraction comparison:", json.dumps(
        {k: comp[k] for k in ("recall_vs_llm_reference",
                              "precision_vs_llm_reference")}, indent=1))

    if not args.no_qa:
        qa = qa_eval(threads)
        (RESULTS / "qa_eval.json").write_text(json.dumps(qa, indent=1))
        print("qa eval:", json.dumps(qa, indent=1))

    print(f"\nresults -> {RESULTS}")


if __name__ == "__main__":
    main()
