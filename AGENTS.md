# AGENTS.md

> Canonical instructions for autonomous coding agents (Codex, Cursor,
> Aider, Claude Code, Jules, Gemini CLI, etc.). `CLAUDE.md` at the repo
> root is a one-line alias to this file — Claude Code's discovery
> convention. The cross-tool spec is <https://agents.md>.

## Project overview

`cortexm` is a deterministic neuro-symbolic memory layer for AI agents.

- 96 bytes per fact on disk; zero LLM call at ingest (μ=0 protocol).
- Provenance on every fact (source span, confidence, `trigger_source`,
  `valid_from` / `valid_to`, BLAKE3 hash chain).
- Surface: `Reader`, `Writer`, `Extractor`, `Bridge`, `TraceStore`,
  `MemoryPalace`, `MCP server`, `REST server`, `SPARQL endpoint`.
- Mem0-compatible public surface: `from cortexm import Memory`.

The Python module is `cortexm/` (rename from `context_m/` landed in this
cycle). A thin `context_m.py` shim is shipped for backward compat with
existing scripts that did `from context_m import Memory`.

## Build and test commands

```bash
pip install -e ".[dev]"                    # editable install + dev deps
pytest -x                                  # full suite, fail-fast
pytest tests/test_research_steals.py -k list   # one file / pattern
ruff check . && ruff format --check .      # lint
python -m cortexm.cli --help               # sanity-check the CLI
python -m cortexm.cli doctor                # SIMD kernels, SQLite ver, deps
```

Tier-1 / Tier-4 benchmark runs (slow, opt-in):

```bash
python benchmarks/run_ood_pipeline.py --personas 4 --skip-render --no-enrich --no-judge
python scripts/longmemeval_judge.py
python scripts/run_beam10m_benchmark.py --config all
```

## Code style

- Lowercase module names; one module per file; no `__init__.py` re-exports
  except the top-level public API in `cortexm/__init__.py`.
- Type hints required on every public function. `from __future__ import
  annotations` at the top of every module.
- Prefer `pathlib.Path` over `os.path`.
- Prefer `dataclass(slots=True)` for value objects.
- Never use `print()` in library code — use `logging` (stdlib) or the
  `cortexm.metrics` counters.
- Never catch `Exception` broadly — name the exception type.
- Determinism is a hard contract: any change that introduces nondeterminism
  (uuid4 tiebreaks, dict-order dependence, RNG without seed) breaks the
  BEAM-honest protocol and must be reverted.
- Every retrieval MUST carry provenance. The `RetrievalResult.provenance`
  field is non-optional.

## Testing instructions

- Tests live in `tests/` mirroring the `cortexm/` package structure.
- A test must assert behaviour, not implementation. Don't pin on internal
  attribute names that are an implementation detail.
- Run the full Tier-1 / Tier-4 benchmark suite before claiming any
  recall number in a PR description or commit message.
- Use the deterministic judge (`--judge det`) for local runs; reserve
  the Gemini judge (`--judge gemini`) for CI — it costs tokens and is
  rate-limited.
- The full suite must be green before push. The worklog at `worklog.md`
  records the last green count.

## Security considerations

- Never `pickle.loads` untrusted bytes. The store uses SQLite + JSON
  columns precisely to avoid pickle.
- The SQLite store is read-only-by-default; writes require explicit
  `mode="rw"` on `TraceStore(...)`.
- The MCP server exposes only the Reader by default; the Writer is
  opt-in via `--allow-writes` on `cortexm serve-mcp`.
- PII redaction is on by default at ingest; agents MUST NOT disable it
  without a recorded justification in the fact's `provenance.pii` field.
- Prompt-injection defense: facts ingested from untrusted text are
  tagged `untrusted=True`; the reader demotes them in ranking and the
  context block formatter escapes their values. Do not bypass.
- ZK-SQL proofs (PLONKish / Halo2-style) are opt-in via
  `cortexm.features.zk`; treat them as experimental until the
  verifying-key fixture suite is complete.

## PR instructions

- Title format: `<area>: <imperative summary>` — e.g.
  `reader: add employment-anchored temporal window`.
- Always link the PR to an issue with `Closes #<n>`.
- The linked issue MUST carry the `accepted` label. PRs without one
  are auto-closed by `.github/workflows/pr-gate.yml` (Mem0-style gate).
  "Closed does not mean rejected" — see CONTRIBUTING.md.
- Squash-merge only; one commit per PR.
- Update `worklog.md` with a new section appended at the bottom
  (template in CONTRIBUTING.md). The worklog is the canonical history.

## Dev environment tips

- Run `python -m cortexm.cli doctor` after first install to verify SIMD
  kernels, SQLite version, and optional deps (BLAKE3, cryptography).
- The worklog at `worklog.md` is the canonical history of what's been
  tried — read it before designing a non-trivial change.
- `docs/ARCHITECTURE.md`, `docs/METHODOLOGY.md`, `docs/BENCHMARKS.md`
  are the three docs that answer most "why" questions.
- `docs/FAILURE_MODES.md` records which phrasings break the μ=0
  extractor — read it before claiming a fix to a Tier-1 weak spot.
- The Rust core at `rust/cortexm-core/` mirrors the hot paths
  (`hash`, `simd`, `slb`, `conv`). The Python path is canonical;
  the Rust path is acceleration only. Parity tests live in
  `tests/test_rust_accel.py` and are skipped if the wheel isn't built.
