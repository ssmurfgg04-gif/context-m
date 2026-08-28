#!/usr/bin/env python3
"""OOD benchmark pipeline — render, evaluate, cross-judge, aggregate.

Stages (each idempotent, artifacts cached under benchmarks/ood/):
  1. manifest   — persona ground truth as LLM-renderable fact lists
  2. render     — node benchmarks/llm/render_ood.mjs (6 styles x N personas)
  3. evaluate   — extraction recall + end-to-end retrieval per style
                  (+ enrichment-recovery variant for hard styles)
  4. judge      — export canonical judge items; node judge_llm.mjs cross-check
  5. aggregate  — benchmarks/results/ood/summary.json (mean +/- std across
                  personas, honest per-style numbers)

Usage:
  python benchmarks/run_ood_pipeline.py [--personas 4] [--skip-render]
      [--no-enrich] [--no-judge] [--styles paraphrase,negation,...]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from context_m.bench.generator import make_persona  # noqa: E402
from context_m.bench.ood import (T0, build_ood_corpus, export_judge_items,  # noqa: E402
                                 export_manifest, extraction_recall,
                                 run_ood_eval)
from context_m.bench.generator import Corpus  # noqa: E402
from context_m.config import Config  # noqa: E402

OOD_DIR = REPO / "benchmarks" / "ood"
RESULTS_DIR = REPO / "benchmarks" / "results" / "ood"
LLM_DIR = REPO / "benchmarks" / "llm"
STYLES = ["paraphrase", "negation", "indirect", "informal",
          "non_english", "code_switch"]
ENRICH_STYLES = ["negation", "indirect", "informal", "non_english",
                 "code_switch"]
PERSONA_SEEDS = [101, 202, 303, 404, 505, 606]


def sh(cmd: list[str]) -> None:
    print(f"+ {' '.join(str(c) for c in cmd)}", flush=True)
    r = subprocess.run(cmd, cwd=str(REPO))
    if r.returncode != 0:
        raise SystemExit(f"command failed: {cmd}")


def render(manifest_path: Path, rendered_path: Path, styles: list[str]) -> None:
    sh(["node", str(LLM_DIR / "render_ood.mjs"), str(manifest_path),
        str(rendered_path), "--style", ",".join(styles) if len(styles) < 6 else "all"])


def load_rendered(path: Path) -> list[dict]:
    rows = [json.loads(l) for l in path.read_text().split("\n") if l.strip()]
    return rows


def merged_corpus(rows: list[dict], personas: list[dict], style: str) -> Corpus:
    by_user = {p.user_id: p for p in personas}
    corpus = Corpus(bucket=f"ood-{style}", target_tokens=0, sessions=[],
                    personas=personas, total_tokens=0)
    for i, r in enumerate(rows):
        if r.get("style") != style or r.get("user_id") not in by_user:
            continue
        c = build_ood_corpus(r, by_user[r["user_id"]], T0,
                             target_tokens=30_000, seed=100 + i)
        corpus.sessions.extend(c.sessions)
        corpus.total_tokens += c.total_tokens
    return corpus


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--personas", type=int, default=4)
    ap.add_argument("--skip-render", action="store_true")
    ap.add_argument("--no-enrich", action="store_true")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--styles", type=str, default=",".join(STYLES))
    ap.add_argument("--target-tokens", type=int, default=30_000,
                    help="distractor volume per persona")
    args = ap.parse_args()

    OOD_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    styles = [s for s in args.styles.split(",") if s in STYLES]

    # ---- personas + manifest --------------------------------------------
    personas = [make_persona(random.Random(PERSONA_SEEDS[i]), i, T0)
                for i in range(args.personas)]
    manifest_path = OOD_DIR / f"manifest_p{args.personas}.json"
    manifest_path.write_text(json.dumps(export_manifest(personas), indent=1))
    print(f"manifest: {manifest_path}")

    rendered_path = OOD_DIR / f"rendered_p{args.personas}.jsonl"
    if not args.skip_render or not rendered_path.exists():
        render(manifest_path, rendered_path, styles)
    rows = load_rendered(rendered_path)
    print(f"rendered rows: {len(rows)}")

    # ---- per-style evaluation --------------------------------------------
    from context_m.bridge.enrich import NodeLLMExtractor

    summary: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_personas": args.personas,
        "styles": {},
        "notes": [
            "OOD scores measure generalization to phrasings the pattern "
            "extractor was never tuned against; renderer omissions are "
            "excluded from extraction recall and reported separately.",
            "ID (in-distribution) numbers in benchmarks/results/ are an "
            "upper bound: the extractor was authored against the "
            "generator's templates.",
        ],
    }
    judge_items: list[dict] = []

    for style in styles:
        srows = [r for r in rows if r.get("style") == style]
        if not srows:
            print(f"!! no rendered rows for style {style}, skipping")
            continue
        # extraction-layer recall per persona
        recalls = [extraction_recall(r, personas[int(r["user_id"][4:])], Config())
                   for r in srows if r["user_id"].startswith("user")]
        # end-to-end retrieval
        corpus = merged_corpus(srows, personas, style)
        enrich_fn = None
        enrich_limit = int(os.environ.get("OOD_ENRICH_LIMIT", "150"))
        if style in ENRICH_STYLES and not args.no_enrich:
            def enrich_fn(memory, _ex=NodeLLMExtractor(), _lim=enrich_limit):  # noqa: F811
                return memory.enrich(extractor=_ex, limit=_lim)
        res = run_ood_eval(corpus, personas, style, enrich_fn=enrich_fn)
        res_dict = res.to_dict()
        judge_items.extend(export_judge_items(res, personas))

        per_persona_overall = [r["recall"] for r in recalls
                               if r["recall"] is not None]
        style_summary = {
            "extraction_recall_mean": round(statistics.mean(per_persona_overall), 4)
            if per_persona_overall else None,
            "extraction_recall_stdev": round(statistics.stdev(per_persona_overall), 4)
            if len(per_persona_overall) > 1 else 0.0,
            "renderer_omissions_mean": round(statistics.mean(
                [r["n_renderer_omitted"] for r in recalls]), 2),
            "end_to_end": {
                "overall": res.overall,
                "per_ability": res.per_ability,
                "n_questions": res.n_questions,
                "facts": res.ingest.get("facts"),
                "corpus_tokens": corpus.total_tokens,
            },
        }
        if hasattr(res, "enriched"):
            style_summary["with_llm_enrichment"] = {
                "overall": res.enriched["overall"],
                "per_ability": res.enriched["per_ability"],
                "report": res.enriched["report"],
            }
        summary["styles"][style] = style_summary
        out = RESULTS_DIR / f"{style}.json"
        out.write_text(json.dumps({
            "summary": style_summary,
            "extraction_per_persona": [r for r in recalls],
            "result": res_dict,
        }, indent=1, default=str))
        print(f"[{style}] extraction_recall="
              f"{style_summary['extraction_recall_mean']} "
              f"end_to_end={res.overall}"
              + (f" enriched={res.enriched['overall']}"
                 if hasattr(res, "enriched") else ""))

    (RESULTS_DIR / "summary.json").write_text(json.dumps(summary, indent=1,
                                                          default=str))

    # ---- canonical LLM judge cross-check ---------------------------------
    if not args.no_judge and judge_items:
        items_path = OOD_DIR / "judge_items.jsonl"
        items_path.write_text(
            "\n".join(json.dumps(i) for i in judge_items) + "\n")
        judged_path = OOD_DIR / "judge_items_scored.jsonl"
        sh(["node", str(LLM_DIR / "judge_llm.mjs"), str(items_path),
            str(judged_path)])
        # agreement analysis: deterministic vs LLM grader
        scored = [json.loads(l) for l in judged_path.read_text().split("\n")
                  if l.strip()]
        by_id = {s["id"]: s for s in scored}
        det_scores, llm_scores, pairs = [], [], []
        by_ability: dict[str, list[float]] = {}
        for it in judge_items:
            s = by_id.get(it["id"])
            if not s or s.get("score") is None:
                continue
            det, llm = it["det_score"], s["score"]
            det_scores.append(det)
            llm_scores.append(llm)
            pairs.append((det, llm))
            by_ability.setdefault(it["ability"], []).append(llm)
        exact = sum(1 for d, l in pairs if abs(d - l) < 1e-9)
        within_half = sum(1 for d, l in pairs if abs(d - l) <= 0.5 + 1e-9)
        agreement = {
            "n_items": len(pairs),
            "det_judge_mean": round(sum(det_scores) / len(det_scores), 4)
            if det_scores else None,
            "llm_judge_mean": round(sum(llm_scores) / len(llm_scores), 4)
            if llm_scores else None,
            "exact_agreement": round(exact / len(pairs), 4) if pairs else None,
            "within_half_point": round(within_half / len(pairs), 4) if pairs else None,
            "llm_judge_per_ability": {a: round(sum(v) / len(v), 4)
                                      for a, v in by_ability.items()},
            "judge_models": sorted({s.get("judge_model") for s in scored
                                    if s.get("judge_model")}),
            "protocol": "BEAM-style context-sufficiency rubric replicated "
                        "with the recorded judge model(s); canonical BEAM "
                        "uses gpt-5 — numbers are NOT directly comparable "
                        "across judge models.",
        }
        (RESULTS_DIR / "llm_judge_crosscheck.json").write_text(
            json.dumps(agreement, indent=1))
        print(f"LLM judge cross-check: {agreement}")

    print(f"\nsummary -> {RESULTS_DIR / 'summary.json'}")


if __name__ == "__main__":
    main()
