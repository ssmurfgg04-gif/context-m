#!/usr/bin/env python3
"""Build the static leaderboard from benchmarks/results/.

Reads every result artifact and emits leaderboard/data.js (embedded JSON)
so the site works from file:// with zero network dependencies. The site's
job is honesty: ID numbers are labelled as an upper bound, OOD and
real-GitHub numbers get top billing, and every table carries its judge
protocol and disclaimers.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESULTS = REPO / "benchmarks" / "results"
OUT = REPO / "leaderboard" / "data.js"


def load(p: Path):
    if p.exists():
        return json.loads(p.read_text())
    return None


def build() -> dict:
    data: dict = {"generated_at": load_meta(), "sources": {}}

    # ---- ID (in-distribution) variance ------------------------------------
    var = load(RESULTS / "variance.json")
    if var:
        id_rows = []
        for bucket, b in var["buckets"].items():
            cm = b["per_system"]["context_m"]
            others = {s: v["mean"] for s, v in b["per_system"].items()
                      if s != "context_m"}
            id_rows.append({
                "bucket": bucket,
                "n_questions": b["n_questions"],
                "mean": cm["mean"], "sd": cm["sd"],
                "seeds": cm["scores"],
                "baselines": others,
            })
        data["sources"]["id"] = {
            "label": "In-distribution (synthetic, template-matched) — "
                     "regression harness, NOT a capability claim",
            "rows": id_rows,
            "disclaimer": "The corpus generator and the pattern extractor "
                          "were authored together; this measures template "
                          "coverage, ceiling by construction.",
        }

    # ---- OOD ---------------------------------------------------------------
    ood = load(RESULTS / "ood" / "summary.json")
    if ood:
        rows = []
        for style, v in ood.get("styles", {}).items():
            rows.append({
                "style": style,
                "extraction_recall": v.get("extraction_recall_mean"),
                "extraction_sd": v.get("extraction_recall_stdev"),
                "renderer_omissions": v.get("renderer_omissions_mean"),
                "e2e": v["end_to_end"]["overall"],
                "per_ability": v["end_to_end"].get("per_ability", {}),
                "n_questions": v["end_to_end"].get("n_questions"),
                "enriched": (v.get("with_llm_enrichment") or {}).get("overall"),
            })
        data["sources"]["ood"] = {
            "label": "OOD paraphrase benchmark — independent LLM re-rendering "
                     "of held-out phrasings (the honest generalization gap)",
            "rows": rows,
            "n_personas": ood.get("n_personas"),
            "disclaimer": "Renderings produced by an LLM (glm-4-plus; "
                          "identity recorded in results JSON); judged by "
                          "the deterministic nugget judge; renderer "
                          "omissions excluded from extraction recall.",
        }

    # ---- real GitHub --------------------------------------------------------
    comp = load(RESULTS / "real_github" / "extraction_comparison.json")
    qa = load(RESULTS / "real_github" / "qa_eval.json")
    if comp:
        data["sources"]["real_github"] = {
            "label": "Real GitHub issue threads — zero-LLM vs LLM extractor "
                     "on real human text",
            "comparison": comp,
            "qa": qa,
        }

    # ---- LLM judge cross-check -----------------------------------------------
    xchk = load(RESULTS / "ood" / "llm_judge_crosscheck.json")
    if xchk:
        data["sources"]["llm_judge_crosscheck"] = xchk

    # ---- rust acceleration scorecard ---------------------------------------
    rust = load(RESULTS / "rust_accel.json")
    if rust:
        data["sources"]["rust_accel"] = {
            "label": "Rust wheels vs NumPy reference — hot-path scorecard",
            "host": rust.get("host"),
            "h64": rust.get("h64_x200"),
            "bind": rust.get("bind_perm"),
            "encode_fact": rust.get("encode_fact"),
            "slb": rust.get("slb_hit"),
            "quadrant_clustered": rust.get("quadrant_clustered"),
            "quadrant_random": rust.get("quadrant_random"),
            "disclaimer": "Same machine, same process, median-of-N. SLB "
                          "is a tie (BLAS is already optimal at 64x768 — "
                          "published as such). Quadrant recall on "
                          "structure-free random corpora collapses — the "
                          "adversarial row is shown, not hidden.",
        }

    # ---- CRDT federation ----------------------------------------------------
    fed = load(RESULTS / "federation.json")
    if fed:
        data["sources"]["federation"] = {
            "label": "CRDT federation — convergence, partition heal, "
                     "anti-entropy cost",
            "scenario": fed.get("scenario"),
            "initial_sync": fed.get("initial_sync"),
            "partition_heal": fed.get("partition_heal"),
            "new_node_join": fed.get("new_node_join"),
            "interpretation": fed.get("interpretation"),
        }

    # ---- micro ---------------------------------------------------------------
    micro = load(RESULTS / "micro.json")
    if micro:
        data["sources"]["micro"] = micro
    return data


def load_meta() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def main() -> None:
    data = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("window.LEADERBOARD_DATA = " + json.dumps(data, indent=1)
                   + ";\n")
    print(f"wrote {OUT} "
          f"({', '.join(data['sources'].keys()) or 'no sources found'})")


if __name__ == "__main__":
    main()
