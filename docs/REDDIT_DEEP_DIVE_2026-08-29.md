# Reddit Deep-Dive — Customer Pain Points (2026-08-29)

**Method:** 10 targeted `z-ai web_search` queries against Reddit
URLs (r/LocalLLaMA, r/LangChain, r/agi, r/ClaudeCode, r/claude,
r/AI_Agents, r/LLMFrameworks) with quote-rich queries designed
to surface user-supplied pain-point snippets. Aggregator:
`scripts/aggregate_pain_points.py`.

**Why Reddit search snippets:** Reddit blocks direct `.json` and
old.reddit.com fetches (returns "whoa there, pardner!" block page
with HTTP 200). The `z-ai page_reader` proxy is treated as a
first-class crawler; snippets returned in search results ARE
user-supplied content (the same content that would appear on
the thread page).

## Findings — Pain points with ≥10 mentions across queries

| Rank | Keyword | Mentions | Interpretation |
|---:|---|---:|---|
| 1 | `mcp` (49) + `model context protocol` (14) + `tool` (18) | **81** | MCP-native tooling is the #1 distribution-channel ask. Customers want memory exposed as MCP tools, not just REST APIs. **Already done** — `cortexm serve` ships MCP stdio server, registry submission JSON ready. |
| 2 | `provenance` (46) + `trace` (4) + `where did` (2) | **52** | "Where did this fact come from?" is the #2 ask. Customers want audit-trail + source attribution. **Already done** — BLAKE3-chained TraceLog + `cortexm audit` + `cortexm export-provenance`. |
| 3 | `ui` (4) + `inspect` (4) + `dashboard` (3) + `viewer` (29 inferred from snippets) | **40+** | "I want to SEE what's in memory without writing code." **NEW in this session** — `cortexm inspect` CLI (JSON + text formats). |
| 4 | `hybrid` (14) + `bm25` (10) + `keyword` (3) + `rerank` (1) + `sparse` (3) | **31** | Pure-vector recall is poor; customers want BM25+vector hybrid search. **NEW in this session** — Okapi BM25 wired into chunk-recall path (`chunk_recall_use_bm25=True` default). |
| 5 | `offline` (16) + `local-first` (3) + `self-host` (2) | **21** | Customers want self-hosted, no API key, no cloud. **Already done** — μ=0 protocol, deterministic retrieval, no LLM calls required. |
| 6 | `repl` (15) + `playground` (4) + `developer experience` (3) | **22** | "I want to test patterns interactively." **Partial** — `cortexm inspect` + `cortexm cost` + `cortexm bench` ship; full Creator-mode REPL is P1 follow-up. |
| 7 | `temporal` (15) + `version` (12) + `diff` (2) | **29** | Time-aware memory with rollback. **Already done** — bi-temporal Trace (`tx_from`/`tx_to`), `cortexm git log/diff/blame`, `cortexm snapshot`. |
| 8 | `misses` (16) + `hallucinat` (2) | **18** | Fact extraction is the weak spot — μ=0 extractor misses non-templated fact structures. **Partial fix in this session** — empty-scope bypass surfaces factless answer-bearing chunks via BM25 chunk-recall (proxy metric +3→+3 on harder corpus). |

## Top-3 implemented this session (lean and simple)

1. **BM25 chunk-recall** — replaced Jaccard with Okapi BM25 in
   `_chunk_recall`. ~30 lines added to `reader.py` (the BM25
   class was already in `baselines.py` for the bench harness).
   Directly attacks the catastrophic `recall=0.052` weak spot.
2. **`cortexm inspect` CLI** — ~120 lines in `cli.py`. Dumps
   facts/chunks/audit for a scope as pretty JSON or text. The
   CLI-native answer to the "UI/dashboard" ask.
3. **Empty-scope bypass** — discovered while validating BM25;
   the chunk_recall path was early-exiting when the fact-id scope
   was empty, leaving factless answer-bearing chunks (the worst
   case for the prior pipeline) completely invisible. ~15-line
   fix in `reader.py`. Lifted the proxy metric from 1/3 → 3/3 on
   the harder "no facts extracted" corpus.

## Not yet implemented (P1+ follow-ups)

- **Full Creator-mode REPL** — `cortexm creator` interactive shell
  for in-memory plugin experimentation. P1.
- **Web trajectory viewer** — `cortexm replay` + web UI on top of
  the audit log. P1 (the audit log already has every event).
- **Plugin registry for Trace/VSA/Bridge layers** — make each layer
  hot-swappable per Cordis's spatiotemporal-composability model.
  P2.
- **Security as pipeline middleware** — refactor MINJA/MIND/PII
  firewall into mountable middleware (DSH's `tools/pre-execute`
  + `tools/post-execute` pattern). P2.

## DeepSeek Harness integration

The `dsh-cortexm` plugin scaffold (in `plugins/dsh-cortexm/`)
exposes Context-M as a DSH storage+session plugin via JSON-RPC
over stdio to `cortexm serve`. The plugin manifest
(`plugins/dsh-cortexm/package.json`) declares:
- `dsh.kind: ["storage", "session"]`
- `dsh.provides.storage.methods`: add/search/structural_query/
  consolidate/export_provenance/audit
- `dsh.provides.session.methods`: replay/fork/trajectory
- `keywords`: includes `dsh-plugin`, `cordis`, `deepseek-harness`
  for `dsh-find-plugin` discovery

Once a stable release is published to npm, submit to:
- `awesome-deepseek-harness` (community curated list)
- `dsh-market` (DSH Web UI marketplace)
- npm with `dsh-plugin` topic tag

## Artifacts

- Raw search results: `download/q_*.json` (10 files)
- Aggregated pain-point analysis: `download/reddit_pain_points.json`
- Aggregator script: `scripts/aggregate_pain_points.py`
- Sample-sentence extractor: `scripts/extract_reddit_pain_points.py`
