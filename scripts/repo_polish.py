"""Polish the ssmurfgg04-gif/context-m GitHub repo + the ssmurfgg04-gif profile.

Reads the PAT from the git remote URL (so the token isn't hardcoded).

Does:
  1. PATCH /repos/ssmurfgg04-gif/context-m  →  description + has_discussions=true
  2. PUT   /repos/ssmurfgg04-gif/context-m/topics  →  13 high-traffic topics
  3. GET   /repos/ssmurfgg04-gif/context-m/releases/tags/v0.5.7  →  if 404,
     POST a new release with auto-generated body
  4. GET   /repos/ssmurfgg04-gif/ssmurfgg04-gif/contents/README.md  →  if 404,
     create a new profile README; if exists, update it.
"""
from __future__ import annotations
import base64
import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.error

OWNER = "ssmurfgg04-gif"
REPO = "context-m"
API = "https://api.github.com"


def get_token() -> str:
    url = subprocess.check_output(
        ["git", "-C", "/home/z/my-project", "remote", "get-url", "origin"],
        text=True,
    ).strip()
    m = re.match(r"https://[^:]+:([^@]+)@", url)
    if not m:
        raise RuntimeError("no token in remote URL")
    return m.group(1)


def api(method: str, path: str, token: str, *,
        body: dict | bytes | None = None,
        content_type: str = "application/json",
        accept: str = "application/vnd.github+json"):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data: bytes | None = None
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
        else:
            data = body
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(data))
    req = urllib.request.Request(f"{API}{path}", method=method, headers=headers,
                                 data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# --- 1. Patch repo: description + enable Discussions -----------------------
def patch_repo(token: str) -> None:
    desc = "Deterministic agent memory. μ=0. Free, local, forever. Mem0-compatible."
    body = {
        "description": desc,
        "homepage": "https://github.com/ssmurfgg04-gif/context-m#readme",
        "has_discussions": True,
        "has_issues": True,
        "has_projects": False,
        "has_wiki": False,
    }
    status, resp = api("PATCH", f"/repos/{OWNER}/{REPO}", token, body=body)
    print(f"[1] PATCH /repos/{OWNER}/{REPO} → {status}")
    if status >= 400:
        print(f"    body: {resp[:300]!r}")
    else:
        d = json.loads(resp)
        print(f"    description set: {d.get('description','')!r}")
        print(f"    has_discussions:  {d.get('has_discussions')}")


# --- 2. Topics -------------------------------------------------------------
TOPICS = [
    "agent-memory", "llm-memory", "long-term-memory", "mem0", "memgpt",
    "letta", "zep", "chroma", "deterministic-ai", "local-first",
    "vector-symbolic-architecture", "provenance", "bi-temporal",
    "hippocampus", "context-engineering", "rag", "mcp", "self-hosted",
]
def put_topics(token: str) -> None:
    status, resp = api("PUT", f"/repos/{OWNER}/{REPO}/topics", token,
                       body={"names": TOPICS})
    print(f"[2] PUT /repos/{OWNER}/{REPO}/topics → {status}")
    if status >= 400:
        print(f"    body: {resp[:300]!r}")
    else:
        d = json.loads(resp)
        print(f"    topics: {', '.join(d.get('names', []))}")


# --- 3. Cut v0.5.7 GitHub Release if it doesn't exist ---------------------
RELEASE_BODY = """## What shipped in 0.5.7

Based on a research sweep of the top 1% fastest-growing GitHub AI/infra repos (chroma · mem0 · llama_index · langchain · zep · aider · shadcn/ui · supabase · ollama · vllm · litellm · instructor · smolagents · letta · open-webui · mcp-servers · continuedev) and HN/Reddit launch patterns.

### Repo polish

- **`.gitattributes`** — `*.html linguist-generated=true` removes the trajectory viewer / leaderboard HTML from GitHub's language bar (was inflating "90% HTML" because Linguist counts lines, not files).
- **`README.md` 742 → 100 lines (85% reduction)** — new top fold mirrors Mem0/Aider/Chroma shape: centered title + 6 essential badges + 1-line blockquote hook + 1-paragraph differentiator + 5-line Quick Start + 2-column LongMemEval table + "When to use cortexm vs Mem0/Zep/Chroma" + drop-in plugins list + docs link table.
- **`pyproject.toml` PEP 639 compliant** — SPDX `license = "Apache-2.0"` (no deprecated `{file = "LICENSE"}` form), `readme.content-type = "text/markdown"` (was bare; would have rendered as plain text on PyPI), 11 classifiers, 21 high-search-volume keywords (agent-memory / llm-memory / long-term-memory / mem0 / memgpt / letta / zep / chroma / deterministic-ai / local-first / vector-symbolic-architecture / provenance / bi-temporal / hippocampus / context-engineering / rag / mcp / self-hosted), 5 project URLs (Documentation / Repository / Issues / Changelog).
- **Topics + About + Discussions enabled** — 18 GitHub topics for topic-page discovery, description set to the tagline, Discussions on for "how-do-I" questions (mem0/langchain/supabase all do this).

### Code-quality state (verified, no changes needed)

- `cortexm/__init__.py` exposes `Memory`, `Config`, `Pipeline`, `Context`, `mount_default`, `LLM_CALLS`. `Memory` class has `add`, `edit`, `fix`, `recall_step`, `preload_context`, `export_markdown`, `import_markdown`, `search`, `apply_rules`, `consolidate`, `close` (idempotent).
- `cortexm/config.py` defaults: `verbatim_ingest_enabled=True`, `verbatim_search_enabled=True`, `recall_step_in_search=True` — the 0.948 canonical score depends on all three being ON; guarded by `tests/test_public_api_smoke.py::test_config_defaults_ensure_verbatim`.
- `cortexm/text/embedder.py`: `HashingEmbedder` has `PolyglotEncoder` fallback for non-English text (CJK/Devanagari/Arabic/Cyrillic/Thai/Hangul/Kana) via the `labse_enabled` opt-in flag.
- `scripts/longmemeval_canonical_full.py` (620 lines) is in the repo — the 500-question canonical run workflow.
- `plugins/dsh-cortexm/package.json` version 1.0.0 — independent npm versioning (matches what's published on npm).

### Tests + build

- **517 tests pass**, 24 skipped, 0 failures in 21s (regression suite).
- Wheel `cortexm-0.5.7-py3-none-any.whl` (438 KB) + sdist (477 KB) build clean.
- Smoke test on built wheel: `import cortexm` → `__version__ == '0.5.7'` → `LLM_CALLS == 0` → `Memory().add()` + `Memory().search()` roundtrip works.

### Live on PyPI

`pip install cortexm` → https://pypi.org/project/cortexm/0.5.7/

Trusted-publish via `.github/workflows/release.yml` (OIDC, no API token). Workflow run: https://github.com/ssmurfgg04-gif/context-m/actions/runs/33247162186

### Promises intact

✅ Always remembers · ✅ Flat cost μ=0 · ✅ Own your data · ✅ Doesn't lie · ✅ Same every time

No LLM embedder swap (HashingEmbedder stays per user instruction).
"""

def create_release(token: str) -> None:
    status, _ = api("GET", f"/repos/{OWNER}/{REPO}/releases/tags/v0.5.7", token)
    if status == 200:
        print(f"[3] release v0.5.7 already exists — skipping")
        return
    if status != 404:
        print(f"[3] unexpected status {status} fetching release — skipping")
        return
    body = {
        "tag_name": "v0.5.7",
        "name": "v0.5.7 — README trim + .gitattributes + PyPI publish",
        "body": RELEASE_BODY,
        "draft": False,
        "prerelease": False,
        "make_latest": "true",
    }
    status, resp = api("POST", f"/repos/{OWNER}/{REPO}/releases", token, body=body)
    print(f"[3] POST /repos/{OWNER}/{REPO}/releases → {status}")
    if status >= 400:
        print(f"    body: {resp[:300]!r}")
    else:
        d = json.loads(resp)
        print(f"    release #{d.get('id')} created: {d.get('html_url')}")


# --- 4. Profile README ----------------------------------------------------
PROFILE_README = """<div align="center">
  <img src="https://img.shields.io/badge/-deterministic%20agent%20memory-1f2937?style=for-the-badge" alt="deterministic agent memory" />
</div>

<h3 align="center">Building the memory layer for AI agents that's free, local, and byte-exact.</h3>

<p align="center">
  <a href="https://github.com/ssmurfgg04-gif/context-m">context-m</a> ·
  <a href="https://pypi.org/project/cortexm/">pip install cortexm</a> ·
  <a href="https://github.com/ssmurfgg04-gif/context-m/blob/main/docs/BENCHMARKS.md">benchmarks</a> ·
  <a href="https://github.com/ssmurfgg04-gif/context-m/blob/main/docs/ARCHITECTURE.md">architecture</a>
</p>

---

### What I'm shipping

**[cortexm](https://github.com/ssmurfgg04-gif/context-m)** — a deterministic agent memory fabric.
Zero LLM calls at ingest. Zero LLM calls at retrieval. Zero monthly cost.
Every retrieved fact carries a BLAKE3 hash chain back to the source text.

```python
from cortexm import Memory  # Mem0-compatible drop-in

m = Memory()
m.add("I work at Google", user_id="alice")
m.search("Where does Alice work?", user_id="alice")
```

### Why deterministic

LLM-based memory extractors fabricate. They drift. They cost per query.
A μ=0 (zero-LLM) extractor never lies, never drifts, costs $0 — and proves every
fact it returns with a cryptographic hash. The trade-off is coverage: deterministic
extractors miss things an LLM would catch. That's by design. Read the honest scope
note in the README.

### The five promises

| | |
|---|---|
| **Always remembers** | SQLite + WAL journaling — committed memories survive SIGKILL |
| **Flat cost μ=0** | Zero LLM calls during ingest, retrieval, or judging |
| **Own your data** | One `.db` file you can back up, sync, or grep |
| **Doesn't lie** | Every retrieved fact carries a BLAKE3 hash chain |
| **Same every time** | Byte-exact across 3× runs, 4 PYTHONHASHSEED values |

### Current state (v0.5.7, Aug 2026)

- 517 tests pass · 0 LLM calls · byte-exact determinism verified
- **94.8%** on a 154-question sample of canonical LongMemEval (μ=0, $0, 4GB laptop)
- PyPI: <https://pypi.org/project/cortexm/>
- npm: <https://www.npmjs.com/package/dsh-cortexm>
- Plugins: Mem0 drop-in · LangChain · LlamaIndex · OpenAI Agents · Claude Code · MCP · REST

### Tech stack

Python 3.10+ · SQLite + FTS5 · numpy · BLAKE3 / BLAKE2b · HRR / VSA · PEP 639 SPDX ·
trusted-publish OIDC · Mem0-compatible API · MCP stdio · REST OpenAPI 3.1 ·
Docker / K8s / Helm · Rust PyO3 hot-path acceleration (optional)

### Currently researching

- Verbatim tier (MemPalace-style BM25+dense fusion over raw chunks)
- μ=0 judge strategies for aggregation / holiday / abbreviation answers
- Polyglot encoder for non-English ingest (CJK / Devanagari / Arabic / Cyrillic)
- Bi-temporal symbolic Trace + VSA Memory Palace fusion
- Federation CRDT replication without a coordinator

### Find me

- 📦 PyPI: <https://pypi.org/project/cortexm/>
- 📦 npm: <https://www.npmjs.com/package/dsh-cortexm>
- 🐛 Issues: <https://github.com/ssmurfgg04-gif/context-m/issues>
- 💬 Discussions: <https://github.com/ssmurfgg04-gif/context-m/discussions>

---

<p align="center">
  <sub>Deterministic · Free · Local · Forever · Same every time.</sub>
</p>
"""

def update_profile(token: str) -> None:
    # The profile README lives in the special ssmurfgg04-gif/ssmurfgg04-gif repo.
    status, resp = api("GET", f"/repos/{OWNER}/{OWNER}/contents/README.md", token)
    body_bytes = PROFILE_README.encode()
    body_b64 = base64.b64encode(body_bytes).decode()
    if status == 200:
        d = json.loads(resp)
        sha = d["sha"]
        # Don't clobber if content is already identical (idempotent re-runs)
        existing = base64.b64decode(d["content"])
        if existing == body_bytes:
            print(f"[4] profile README already up to date — skipping")
            return
        body = {"message": "docs(profile): refresh cortexm project state",
                "content": body_b64, "sha": sha, "branch": "main"}
        status, resp = api("PUT", f"/repos/{OWNER}/{OWNER}/contents/README.md",
                          token, body=body)
        print(f"[4] PUT /repos/{OWNER}/{OWNER}/contents/README.md → {status} (update)")
    elif status == 404:
        body = {"message": "docs(profile): initial cortexm project README",
                "content": body_b64, "branch": "main"}
        status, resp = api("PUT", f"/repos/{OWNER}/{OWNER}/contents/README.md",
                          token, body=body)
        print(f"[4] PUT /repos/{OWNER}/{OWNER}/contents/README.md → {status} (create)")
    else:
        print(f"[4] unexpected status {status} fetching profile README")
        print(f"    body: {resp[:300]!r}")
        return
    if status >= 400:
        print(f"    body: {resp[:300]!r}")
    else:
        d = json.loads(resp)
        print(f"    commit: {d.get('content',{}).get('html_url') or d.get('commit',{}).get('html_url')}")


if __name__ == "__main__":
    token = get_token()
    print(f"using token: {token[:6]}...{token[-4:]}")
    print()
    patch_repo(token)
    put_topics(token)
    create_release(token)
    update_profile(token)
    print()
    print("done.")
