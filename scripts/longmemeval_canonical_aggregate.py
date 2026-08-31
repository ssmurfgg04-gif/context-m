"""Aggregate canonical LongMemEval slices into one full-500 score.

Takes multiple slice JSON files (each produced by
`scripts/longmemeval_canonical_full.py --start X --end Y`) and merges
their per-question results into a single canonical 500-Q score.

v0.6.5 CONTAMINATION GUARD: the v0.6.4 full-500 aggregate silently
mixed STALE partial slices (committed earlier, scored with older
code) into the fresh run because the workflow glob
``canonical_slice_*.json`` matched them all and "later slice wins"
let stale files override fresh artifacts — 100 questions carried
v0.6.3-era results and the published 0.944 was actually 0.958. The
aggregate now:
  * reports every duplicate qid across input files (loud table)
  * records which FILE supplied each result + per-file counts
  * stamps git sha + aggregation timestamp into the summary
  * exits nonzero when duplicates would change the score
    (--allow-duplicates to override, never silent)

Usage:
  python scripts/longmemeval_canonical_aggregate.py \\
      --slices benchmarks/full500_slices/canonical_slice_*.json \\
      --out benchmarks/results/canonical_full.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path


def _git_sha() -> str:
    """Best-effort git sha of the working tree (for provenance)."""
    for cmd in ("git rev-parse HEAD",
                "git rev-parse --short HEAD"):
        try:
            out = subprocess.run(cmd.split(), capture_output=True,
                                 text=True, timeout=5)
            if out.returncode == 0:
                return out.stdout.strip()
        except Exception:
            continue
    return "unknown"


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
              full_n: int = 500,
              allow_duplicates: bool = False) -> dict:
    """Merge slices into a single canonical full-N summary.

    v0.6.5: duplicate qids across input files are surfaced loudly
    (file, verdict flip) instead of silently resolved. If duplicates
    would change the score, exit nonzero unless
    ``allow_duplicates=True``.
    """
    by_idx: dict[int, dict] = {}
    source_file: dict[int, str] = {}
    dup_report: dict[int, list[tuple[str, bool]]] = defaultdict(list)
    flips: list[tuple[int, str, str]] = []
    for path in slices:
        results = _load_slice(path)
        print(f"[slice] {path}: {len(results)} questions")
        for r in results:
            idx = r.get("global_idx")
            if idx is None:
                continue
            dup_report[idx].append(
                (os.path.basename(path), bool(r.get("det_correct"))))
            if idx in by_idx:
                prev = by_idx[idx]
                if prev.get("det_correct") != r.get("det_correct"):
                    flips.append((idx, source_file[idx],
                                  os.path.basename(path)))
                # later slice wins on conflict (kept for explicit
                # override workflows; the guard above makes it loud)
            by_idx[idx] = r
            source_file[idx] = os.path.basename(path)

    dups = {k: v for k, v in dup_report.items() if len(v) > 1}
    if dups:
        print(f"\n[DUPLICATES] {len(dups)} qids appear in multiple "
              f"slice files:", file=sys.stderr)
        for idx, entries in sorted(dups.items())[:20]:
            files = ", ".join(f"{f}({'T' if ok else 'F'})"
                              for f, ok in entries)
            print(f"  idx {idx}: {files}", file=sys.stderr)
        if len(dups) > 20:
            print(f"  ... and {len(dups) - 20} more", file=sys.stderr)
    if flips:
        print(f"\n[VERDICT FLIPS] {len(flips)} qids have CONFLICTING "
              f"verdicts across slices:", file=sys.stderr)
        for idx, f1, f2 in flips[:20]:
            print(f"  idx {idx}: {f1} vs {f2}", file=sys.stderr)
        if not allow_duplicates:
            print("\nRefusing to aggregate silently — stale slice files "
                  "are likely mixed with fresh ones (this exact bug "
                  "produced the bogus 0.944 in v0.6.4). Clean the input "
                  "glob or pass --allow-duplicates.", file=sys.stderr)
            sys.exit(2)

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
        "aggregate_provenance": {
            "git_sha": _git_sha(),
            "aggregated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                            time.gmtime()),
            "slice_files": {
                os.path.basename(p): len(_load_slice(p))
                for p in slices
            },
            "duplicate_qids": len(dups),
            "verdict_flips": len(flips),
        },
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
    ap.add_argument("--allow-duplicates", action="store_true",
                    help="permit duplicate qids across slices (loud, "
                         "never silent; exits 2 on verdict flips by "
                         "default)")
    args = ap.parse_args()
    # expand globs
    slices = []
    for s in args.slices:
        if any(c in s for c in "*?["):
            slices.extend(sorted(glob.glob(s)))
        else:
            slices.append(s)
    aggregate(slices, out_path=args.out, full_n=args.full_n,
              allow_duplicates=args.allow_duplicates)
