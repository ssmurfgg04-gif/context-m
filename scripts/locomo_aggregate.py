"""Aggregate LoCoMo per-conversation shard results into one full score.

Mirrors the v0.6.5 full-500 aggregate hygiene (the lesson that produced
the contamination guard after the bogus 0.944):
  * reports every duplicate qid across input files (loud table)
  * records which FILE supplied each result + per-file counts
  * stamps git sha + aggregation timestamp into the summary
  * exits nonzero (2) when duplicates would change the score
    (--allow-duplicates to override, never silent)

Input: per-conversation runner outputs (scripts/locomo_canonical.py
--conv-indices N --out ...). Output: a single canonical
benchmarks/results/locomo/locomo_full.json-shaped file.

Usage:
  python scripts/locomo_aggregate.py \\
      --shards benchmarks/locomo_shards/locomo_conv_*.json \\
      --out benchmarks/results/locomo/locomo_full.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.locomo_canonical import summarize, provenance


def _git_sha() -> str:
    for cmd in ("git rev-parse HEAD", "git rev-parse --short HEAD"):
        try:
            out = subprocess.run(cmd.split(), capture_output=True,
                                 text=True, timeout=5)
            if out.returncode == 0:
                return out.stdout.strip()
        except Exception:
            continue
    return "unknown"


def _load_convs(path: str) -> list[dict]:
    if not os.path.exists(path):
        print(f"  [warn] shard not found: {path}", file=sys.stderr)
        return []
    with open(path) as f:
        d = json.load(f)
    if isinstance(d, dict):
        return d.get("results", [])
    return []


def aggregate(shards: list[str], out_path: str | None = None,
              full_n_conv: int = 10,
              allow_duplicates: bool = False) -> dict:
    """Merge per-conversation shards into one canonical summary."""
    by_cid: dict[str, dict] = {}
    source_file: dict[str, str] = {}
    dup_report: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    flips: list[tuple[str, str, str]] = []

    for path in shards:
        convs = _load_convs(path)
        print(f"[shard] {os.path.basename(path)}: "
              f"{len(convs)} conversation(s)")
        for conv in convs:
            cid = conv.get("conversation_id")
            n_ok = sum(1 for r in conv.get("results", [])
                       if r.get("det_correct"))
            n_tot = len(conv.get("results", []))
            dup_report[cid].append(
                (os.path.basename(path), n_ok, n_tot))
            if cid in by_cid:
                prev_ok = sum(1 for r in by_cid[cid].get("results", [])
                              if r.get("det_correct"))
                if prev_ok != n_ok:
                    flips.append((cid, source_file[cid],
                                  os.path.basename(path)))
                # later shard wins (kept for explicit override; the
                # guard above makes it loud, never silent)
            by_cid[cid] = conv
            source_file[cid] = os.path.basename(path)

    dups = {k: v for k, v in dup_report.items() if len(v) > 1}
    if dups:
        print(f"\n[DUPLICATES] {len(dups)} conversations appear in "
              f"multiple shard files:", file=sys.stderr)
        for cid, entries in sorted(dups.items())[:20]:
            files = ", ".join(f"{f}({ok}/{tot})"
                              for f, ok, tot in entries)
            print(f"  {cid}: {files}", file=sys.stderr)
    if flips:
        print(f"\n[VERDICT FLIPS] {len(flips)} conversations have "
              f"CONFLICTING scores across shards:", file=sys.stderr)
        for cid, f1, f2 in flips[:20]:
            print(f"  {cid}: {f1} vs {f2}", file=sys.stderr)
        if not allow_duplicates:
            print("\nRefusing to aggregate silently — stale shard files "
                  "are likely mixed with fresh ones (see worklog Task "
                  "18/19: this exact pattern produced the bogus 0.944). "
                  "Clean the input glob or pass --allow-duplicates.",
                  file=sys.stderr)
            sys.exit(2)

    convs = sorted(by_cid.values(), key=lambda c: c.get("conv_idx", 0))
    print(f"\n[aggregate] {len(convs)} conversations "
          f"(target {full_n_conv})")

    summary = summarize(convs)
    summary["aggregate_provenance"] = {
        "git_sha": _git_sha(),
        "n_shard_files": len(shards),
        "per_file_counts": {os.path.basename(p): len(_load_convs(p))
                            for p in shards},
    }

    if out_path:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        payload = {"summary": summary, "provenance": provenance(),
                   "results": convs}
        tmp = out_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, out_path)
        print(f"Saved {out_path}")

    print("\n" + "=" * 64)
    print(f" LoCoMo aggregate — {summary['n_questions']} questions, "
          f"{len(convs)} conversations")
    print("=" * 64)
    print(f"  comparable (single/multi/temporal): "
          f"{summary['comparable_subset']['accuracy']} "
          f"({summary['comparable_subset']['counts'][0]}/"
          f"{summary['comparable_subset']['counts'][1]})")
    for k, v in summary["by_category"].items():
        cnt = summary["by_category_counts"][k]
        print(f"  {k:<24} {v}  ({cnt[0]}/{cnt[1]})")
    print("=" * 64)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shards", nargs="*", default=[])
    ap.add_argument("--out", type=str,
                    default="benchmarks/results/locomo/locomo_full.json")
    ap.add_argument("--full-n-conv", type=int, default=10)
    ap.add_argument("--allow-duplicates", action="store_true")
    args = ap.parse_args()
    shards = []
    for pat in args.shards:
        shards.extend(sorted(glob.glob(pat)))
    if not shards:
        print("no shard files matched", file=sys.stderr)
        sys.exit(1)
    aggregate(shards, out_path=args.out,
              full_n_conv=args.full_n_conv,
              allow_duplicates=args.allow_duplicates)


if __name__ == "__main__":
    main()
