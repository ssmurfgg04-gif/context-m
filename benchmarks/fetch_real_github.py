#!/usr/bin/env python3
"""Fetch REAL conversational data from GitHub for the OOD benchmark.

The in-distribution corpus is synthetic; the reviewer's core objection.
This script pulls real issue threads (real humans, real facts: versions,
OSes, tools, timelines, status changes) from public repos via the REST API
and stores them as JSONL with full provenance (repo, issue number, URL,
authors, timestamps) so every benchmark number traces back to real data.

Usage:
  python benchmarks/fetch_real_github.py [--repos r/r,...] [--threads 8]
      [--out benchmarks/real_github/threads.jsonl]

Data policy: public repo metadata via the unauthenticated GitHub REST API;
threads are attributed (repo + issue URL) in every artifact; no PII beyond
public usernames.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DEFAULT = REPO / "benchmarks" / "real_github" / "threads.jsonl"

DEFAULT_REPOS = [
    "rust-lang/rust",
    "numpy/numpy",
    "pydantic/pydantic",
]

API = "https://api.github.com"


def _get(url: str, retries: int = 5) -> dict | list:
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "context-m-benchmark-fetch/1.0",
    })
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):  # rate limited — wait out the window
                wait = 20 * (attempt + 1)
                print(f"  rate-limited on {url}; sleeping {wait}s",
                      file=sys.stderr)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"exhausted retries for {url}")


def fetch_threads(repos: list[str], n_threads: int) -> list[dict]:
    threads: list[dict] = []
    per_repo = max(2, n_threads // len(repos))
    for repo in repos:
        print(f"listing issues for {repo}...")
        issues = _get(f"{API}/repos/{repo}/issues?state=all&sort=comments"
                      f"&direction=desc&per_page={per_repo * 3}")
        picked = 0
        for it in issues:
            if picked >= per_repo:
                break
            if "pull_request" in it:      # skip PRs — we want issue threads
                continue
            n = it["number"]
            print(f"  fetching comments for #{n} "
                  f"({it['comments']} comments)...")
            comments = _get(f"{API}/repos/{repo}/issues/{n}/comments"
                            f"?per_page=30")
            # real humans, enough conversational depth to be worth ingesting
            human = [c for c in comments
                     if not c["user"]["login"].endswith("[bot]")]
            if len(human) < 3:
                continue
            threads.append({
                "id": f"{repo.replace('/', '_')}#{n}",
                "repo": repo,
                "number": n,
                "title": it["title"],
                "url": it["html_url"],
                "state": it["state"],
                "created_at": it["created_at"],
                "author": it["user"]["login"],
                "body": it.get("body") or "",
                "comments": [{
                    "author": c["user"]["login"],
                    "created_at": c["created_at"],
                    "body": c.get("body") or "",
                } for c in human],
            })
            picked += 1
    return threads


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", default=",".join(DEFAULT_REPOS))
    ap.add_argument("--threads", type=int, default=9)
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    args = ap.parse_args()

    repos = [r.strip() for r in args.repos.split(",") if r.strip()]
    threads = fetch_threads(repos, args.threads)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        for t in threads:
            fh.write(json.dumps(t) + "\n")
    n_comments = sum(len(t["comments"]) for t in threads)
    print(f"wrote {len(threads)} threads ({n_comments} comments) -> {out}")
    # provenance sidecar
    prov = {
        "fetched_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repos": repos,
        "threads": [{"id": t["id"], "url": t["url"], "repo": t["repo"],
                     "title": t["title"]} for t in threads],
        "license_note": "Public GitHub issue threads fetched via the "
                        "unauthenticated REST API; used for benchmark "
                        "evaluation with attribution, not redistributed "
                        "as a dataset.",
    }
    out.with_suffix(".provenance.json").write_text(json.dumps(prov, indent=1))


if __name__ == "__main__":
    main()
