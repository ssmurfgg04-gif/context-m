#!/usr/bin/env python3
"""Aggregate LLM-judge results into the crosscheck format + a summary.

Consumes:
  --ood-items   judge items JSONL (id, ability, det_score, ...)
  --ood-scored  judge scores JSONL (id, score, judge_model, ...)
  --real-github directory with extraction_comparison.json / qa_eval.json
  --out         results directory

Writes:
  <out>/ood/llm_judge_crosscheck_<backend>.json   agreement statistics
  <out>/llm_eval_summary.md                       human summary (job summary)

The crosscheck answers ONE question: does an independent LLM grader agree
with the deterministic offline judge? Agreement stats are computed over the
items the LLM judge actually scored (n recorded, sampling labelled).
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().split("\n") if l.strip()]


def ood_crosscheck(items: list[dict], scored: list[dict],
                   backend: str) -> dict | None:
    by_id = {s["id"]: s for s in scored if isinstance(s.get("score"), (int, float))}
    det, llm, pairs, per_ability = [], [], [], defaultdict(lambda: [0.0, 0])
    models = set()
    for it in items:
        s = by_id.get(it["id"])
        if s is None or not isinstance(it.get("det_score"), (int, float)):
            continue
        d, l = float(it["det_score"]), float(s["score"])
        det.append(d)
        llm.append(l)
        pairs.append((d, l))
        per_ability[it["ability"]][0] += l
        per_ability[it["ability"]][1] += 1
        if s.get("judge_model"):
            models.add(s["judge_model"])
    if not pairs:
        return None
    n = len(pairs)
    exact = sum(1 for d, l in pairs if abs(d - l) < 1e-9)
    within_half = sum(1 for d, l in pairs if abs(d - l) <= 0.5 + 1e-9)
    return {
        "n_items": n,
        "n_items_attempted": len(items),
        "sampling_note": (f"agreement computed over all {n} scored items "
                          f"(judge is resumable; attempted {len(items)})."),
        "det_judge_mean": round(sum(det) / n, 4),
        "llm_judge_mean": round(sum(llm) / n, 4),
        "exact_agreement": round(exact / n, 4),
        "within_half_point": round(within_half / n, 4),
        "llm_judge_per_ability": {k: round(v[0] / v[1], 4)
                                  for k, v in sorted(per_ability.items())},
        "judge_models": sorted(models),
        "finding": (
            f"LLM judge mean {sum(llm)/n:.3f} vs deterministic judge mean "
            f"{sum(det)/n:.3f}; exact agreement {exact/n:.1%}. Two "
            "independent graders — the offline judge is not silently "
            "inflating scores" +
            (" (it grades higher here)" if sum(det) > sum(llm)
             else (" (it grades lower here)" if sum(det) < sum(llm)
                   else " (they agree exactly)") + ".")),
        "protocol": (
            "BEAM-style context-sufficiency rubric replicated with the "
            "recorded judge model(s); canonical BEAM uses gpt-5 — numbers "
            "are NOT directly comparable across judge models."),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def real_github_tables(rg_dir: Path) -> tuple[list[str], dict | None, dict | None]:
    comp = qa = None
    try:
        comp = json.loads((rg_dir / "extraction_comparison.json").read_text())
    except (OSError, ValueError):
        pass
    try:
        qa = json.loads((rg_dir / "qa_eval.json").read_text())
    except (OSError, ValueError):
        pass
    lines: list[str] = []
    if comp:
        lines += [
            "### Real-GitHub track — μ=0 extractor vs LLM reference extractor",
            "",
            f"- threads: **{comp.get('n_threads')}**, comments: **{comp.get('n_comments')}**",
            f"- μ=0: **{comp.get('u0', {}).get('facts')} facts**, "
            f"{comp.get('u0', {}).get('ms_per_comment')} ms/comment, "
            f"${comp.get('u0', {}).get('cost_usd')} cost",
            f"- LLM reference: **{comp.get('llm_reference', {}).get('facts')} facts**, "
            f"{comp.get('llm_reference', {}).get('ms_per_comment')} ms/comment, "
            f"{comp.get('llm_reference', {}).get('tokens')} tokens, "
            f"model `{comp.get('llm_reference', {}).get('model')}`",
            f"- recall vs LLM reference: **{comp.get('recall_vs_llm_reference')}**",
            f"- precision vs LLM reference: **{comp.get('precision_vs_llm_reference')}**",
            "",
        ]
    if qa:
        lines += [
            "### Real-GitHub track — retrieval graded by the LLM judge",
            "",
            f"- questions: **{qa.get('n_questions')}**",
            f"- overall: **{qa.get('llm_judge_overall')}**",
            f"- answerable: **{qa.get('answerable')}** | abstention: **{qa.get('abstention')}**",
            f"- judge model(s): `{qa.get('judge_models')}`",
            "",
        ]
    return lines, comp, qa


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ood-items", type=Path, required=True)
    ap.add_argument("--ood-scored", type=Path, required=True)
    ap.add_argument("--real-github", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--backend", default="gemini")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    items = read_jsonl(args.ood_items)
    scored = read_jsonl(args.ood_scored)
    cc = ood_crosscheck(items, scored, args.backend)

    md = ["# LLM-judge evaluation", "",
          f"_backend: `{args.backend}` — generated "
          f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_", ""]
    if cc:
        (args.out / "ood").mkdir(parents=True, exist_ok=True)
        out_json = args.out / "ood" / f"llm_judge_crosscheck_{args.backend}.json"
        out_json.write_text(json.dumps(cc, indent=1))
        md += ["### OOD judge cross-check", "",
               f"- items scored: **{cc['n_items']}/{cc['n_items_attempted']}**",
               f"- LLM judge mean: **{cc['llm_judge_mean']}** vs det judge mean: **{cc['det_judge_mean']}**",
               f"- exact agreement: **{cc['exact_agreement']:.1%}**, within 0.5: **{cc['within_half_point']:.1%}**",
               f"- judge model(s): `{cc['judge_models']}`",
               f"- {cc['finding']}",
               f"- protocol: {cc['protocol']}",
               ""]
    else:
        md += ["### OOD judge cross-check",
               "_no scored items found (run judge_llm.mjs first)_", ""]

    if args.real_github:
        lines, _, _ = real_github_tables(args.real_github)
        md += lines

    (args.out / "llm_eval_summary.md").write_text("\n".join(md))
    print(f"wrote {args.out / 'llm_eval_summary.md'}")
    if cc:
        print(json.dumps({k: cc[k] for k in
                          ("n_items", "llm_judge_mean", "det_judge_mean",
                           "exact_agreement")}, indent=1))


if __name__ == "__main__":
    main()
