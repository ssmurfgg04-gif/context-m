"""Aggregate canonical LongMemEval slices into one full-500 score.

Takes multiple slice JSON files (each produced by
`scripts/longmemeval_canonical_full.py --start X --end Y`) and merges
their per-question results into a single canonical 500-Q score.

Dedupes by `global_idx` (later slice wins on conflict — but in
practice slices shouldn't overlap).

Outputs:
  - overall accuracy across all questions actually run
  - per-subtask accuracy (the only number that matters for diagnosis)
  - per-strategy accuracy (which judge path fired)
  - failure count (errors)
  - missing question indices (for follow-up runs)

Usage:
  python scripts/longmemeval_canonical_aggregate.py \\
      --slices benchmarks/results/canonical_slice_0.json \\
               benchmarks/results/canonical_slice_100.json \\
      --out benchmarks/results/canonical_full.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from pathlib import Path


def _load_slice(path: str) -> list[dict]:
    """Load a slice file and return its results list."""
    if not os.path.exists(path):
        print(f"  [warn] slice not found: {path}", file=sys.stderr)
        return []
    with open(path) as f:
        d = json.load(f)
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        return d.get("results", [])
    return []


def aggregate(slices: list[str], out_path: str | None = None,
              full_n: int = 500) -> dict:
    """Merge slices into a single canonical full-N summary."""
    by_idx: dict[int, dict] = {}
    for path in slices:
        results = _load_slice(path)
        print(f"[slice] {path}: {len(results)} questions")
        for r in results:
            idx = r.get("global_idx")
            if idx is None:
                continue
            by_idx[idx] = r  # later slice wins on conflict

    all_results = sorted(by_idx.values(), key=lambda r: r["global_idx"])
    print(f"\n[aggregate] {len(all_results)} unique questions across "
          f"{len(slices)} slices (target {full_n})")

    # Per-subtask
    by_sub: dict[str, list[float]] = defaultdict(list)
    by_strat: dict[str, list[float]] = defaultdict(list)
    errors = 0
    for r in all_results:
        by_sub[r["subtask"]].append(1.0 if r.get("det_correct") else 0.0)
        by_strat[r.get("judge_strategy", "?")].append(
            1.0 if r.get("det_correct") else 0.0)
        if r.get("judge_strategy") == "error":
            errors += 1

    det_score = sum(1.0 if r.get("det_correct") else 0.0
                    for r in all_results) / max(len(all_results), 1)

    # Per-subtask breakdown — the diagnostic number
    sub_breakdown = {}
    for k, v in by_sub.items():
        sub_breakdown[k] = {
            "n": len(v),
            "accuracy": round(sum(v) / len(v), 4) if v else 0.0,
            "correct": int(sum(v)),
            "wrong": int(len(v) - sum(v)),
        }

    # Missing indices (for follow-up runs)
    have = set(by_idx.keys())
    missing = sorted(set(range(full_n)) - have)

    summary = {
        "n_total_questions": len(all_results),
        "n_target": full_n,
        "coverage_pct": round(100 * len(all_results) / full_n, 2),
        "det_judge_accuracy": round(det_score, 4),
        "by_subtask": sub_breakdown,
        "by_strategy": {k: {"n": len(v),
                            "accuracy": round(sum(v) / len(v), 4)}
                        for k, v in by_strat.items()},
        "judged_by": "deterministic_rule",
        "mempalace_target": 0.966,
        "mempalace_parity": det_score >= 0.966,
        "errors": errors,
        "missing_indices": missing[:50] + (["..."] if len(missing) > 50 else []),
        "n_missing": len(missing),
        "honest_scope_note": (
            f"Aggregated {len(all_results)} / {full_n} canonical "
            f"questions from {len(slices)} slice(s). μ=0 throughout. "
            f"Score is REAL for the questions actually run; "
            f"if coverage < 100%, the score is a SAMPLE of the "
            f"canonical 500, not the full-500 number."
        ),
    }

    print("\n" + "=" * 60)
    print(f" Canonical LongMemEval (aggregated) — "
          f"{len(all_results)} / {full_n} questions")
    print("=" * 60)
    print(f"  coverage: {summary['coverage_pct']}%")
    print(f"  det_judge_accuracy: {summary['det_judge_accuracy']}")
    print(f"  errors: {errors}")
    print(f"  by_subtask:")
    for k, v in summary["by_subtask"].items():
        print(f"    {k}: n={v['n']} acc={v['accuracy']} "
              f"(correct={v['correct']}, wrong={v['wrong']})")
    print(f"  by_strategy:")
    for k, v in summary["by_strategy"].items():
        print(f"    {k}: n={v['n']} acc={v['accuracy']}")
    print(f"  missing_indices: {len(missing)}")
    print("=" * 60)

    if out_path:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w") as f:
            json.dump({"summary": summary, "results": all_results},
                      f, indent=2)
        print(f"\nResults saved to {out_path}")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--slices", nargs="+", required=True,
                    help="slice JSON files to merge")
    ap.add_argument("--out", type=str,
                    default="benchmarks/results/canonical_full.json")
    ap.add_argument("--full-n", type=int, default=500,
                    help="target total (default 500)")
    args = ap.parse_args()
    # expand globs
    slices = []
    for s in args.slices:
        if any(c in s for c in "*?["):
            slices.extend(sorted(glob.glob(s)))
        else:
            slices.append(s)
    aggregate(slices, out_path=args.out, full_n=args.full_n)
