"""Convert a checkpoint JSONL file to a slice-style JSON.

When a slice run gets killed mid-way (no final .json written), the
checkpoint JSONL still has all the per-question results. This script
converts that checkpoint file into the JSON format the aggregator
expects, so partial-slice results aren't lost.
"""
import argparse
import json
import os
import sys
from pathlib import Path


def checkpoint_to_json(ckpt_path: str, out_path: str | None = None) -> dict:
    """Read a .ckpt.jsonl and write a .json the aggregator accepts."""
    if not os.path.exists(ckpt_path):
        print(f"checkpoint not found: {ckpt_path}", file=sys.stderr)
        return {}
    results = []
    with open(ckpt_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                results.append(json.loads(line))
            except Exception:
                continue
    if not results:
        print(f"checkpoint empty: {ckpt_path}", file=sys.stderr)
        return {}
    # Compute summary inline (mirrors run_full_canonical)
    by_sub: dict[str, list[float]] = {}
    by_strat: dict[str, list[float]] = {}
    for r in results:
        by_sub.setdefault(r["subtask"], []).append(
            1.0 if r.get("det_correct") else 0.0)
        by_strat.setdefault(r.get("judge_strategy", "?"), []).append(
            1.0 if r.get("det_correct") else 0.0)
    det_score = sum(1.0 if r.get("det_correct") else 0.0
                    for r in results) / max(len(results), 1)
    summary = {
        "n_questions": len(results),
        "slice_start": min(r["global_idx"] for r in results),
        "slice_end": max(r["global_idx"] for r in results) + 1,
        "det_judge_accuracy": round(det_score, 4),
        "by_subtask": {k: round(sum(v) / len(v), 4)
                       for k, v in by_sub.items()},
        "by_strategy": {k: round(sum(v) / len(v), 4)
                        for k, v in by_strat.items()},
        "judged_by": "deterministic_rule",
        "partial": True,  # flag: not all questions in range completed
        "honest_scope_note": (
            f"PARTIAL slice — {len(results)} questions completed "
            f"(run was killed mid-slice). μ=0 throughout. Score is "
            f"REAL for the questions actually run."),
    }
    if not out_path:
        # default: replace .ckpt.jsonl with .json in same dir
        out_path = ckpt_path.replace(".ckpt.jsonl", ".json")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"summary": summary, "results": results}, f, indent=2)
    print(f"wrote {len(results)} results to {out_path}")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("checkpoint", help=".ckpt.jsonl file to convert")
    ap.add_argument("--out", default=None,
                    help="output .json path (default: replace ext)")
    args = ap.parse_args()
    checkpoint_to_json(args.checkpoint, args.out)
