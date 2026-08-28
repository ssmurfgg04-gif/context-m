# Contributing to Context-M

Thanks for taking the time to contribute. This document is the contract
between you and the maintainers. It is short on purpose.

> **TL;DR** — open an issue first, get it labeled `accepted`, then open
> a PR that says `Closes #<n>`. PRs without an accepted issue are
> auto-closed by the [PR Gate](#pr-gate) workflow. **Closed does not
> mean rejected.**

## Code of conduct

Be specific, be honest, be kind. The repo records what was tried and
what failed in `worklog.md`; disagreements are welcome, ad-hominem
attacks are not.

## Issues

- Search existing issues before opening a new one.
- A good issue names the version, includes a runnable reproduction, and
  shows the real output or traceback you saw. Bugs without a repro will
  be closed with the `needs-repro` label.
- Feature requests are welcome; the maintainers add the `accepted`
  label when we agree to take them on. **Only issues labeled `accepted`
  are eligible for linked PR review** — see the PR Gate section below.

## Pull requests

- Branch from `main`; rebase before opening if `main` has moved.
- Title format: `<area>: <imperative summary>` (e.g.
  `reader: add employment-anchored temporal window`).
- Description must include `Closes #<n>` linking the accepted issue.
- Squash-merge only; one commit per PR.
- The full test suite must be green before push:
  `pytest -x` from the repo root.
- Update `worklog.md` (template below) with what you changed, why, and
  any honest benchmark numbers if you touched the retrieval path.

## PR Gate

This repo runs [.github/workflows/pr-gate.yml](.github/workflows/pr-gate.yml)
(Mem0-style gate). When you open a PR:

1. The gate checks whether any linked issue carries the `accepted`
   label.
2. If yes: the PR stays open, ready for review.
3. If no: the gate posts a comment with the `<!-- pr-gate -->` marker
   and closes the PR. **Closing is a queue operation, not a rejection.**
4. When a maintainer labels the linked issue `accepted`, the gate
   reopens the PR automatically. You do nothing.

Exemptions:

- Documentation-only PRs (anything under `docs/` plus `README.md`,
  `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`,
  `SECURITY.md`) skip the gate entirely.
- Bots, same-repo branch pushes, and OWNER/MEMBER/COLLABORATOR
  authors skip the gate.
- PRs opened before `2026-08-28T00:00:00Z` are grandfathered.

## Instructions for autonomous coding agents

> The cross-tool canonical instructions live in
> [AGENTS.md](AGENTS.md). `CLAUDE.md` is a one-line alias.
> This section is the human-facing summary; agents should read
> `AGENTS.md` directly.

If you are an autonomous coding agent (Claude Code, Codex, Cursor,
Aider, Jules, Gemini CLI, etc.) operating on this repository, follow
these rules. They exist because poorly-written agent instructions
**reduce** agent success rates by ~20% (Feb 2026 arXiv study) — these
rules are the escape hatch.

### 1. Read before you write

- Read `worklog.md` (last 200 lines at minimum) before any non-trivial
  change. The worklog records what was tried and what failed. You will
  reproduce a previous failure if you skip this.
- Read `docs/FAILURE_MODES.md` before touching the extractor — it
  records which phrasings break the μ=0 path.
- Read `docs/METHODOLOGY.md` before claiming any benchmark number.

### 2. Determinism is a hard contract

- The μ=0 protocol requires zero LLM calls at ingest and retrieval. Any
  change that introduces an LLM call into the ingest or retrieval path
  is a critical regression, not an optimization.
- Never tiebreak on uuid4 — fact ids are random per process. Use the
  content-key (`(subject, relation, value, valid_from)`). See
  `cortexm/bridge/reader.py::_content_key`.
- Never rely on dict insertion order across processes. Python dicts are
  insertion-ordered but the order is process-dependent when populated
  from set iteration or random queries.
- Any RNG must be seeded. `random.seed(...)` at the top of the test or
  script, or use `numpy.random.default_rng(seed)`.

### 3. Provenance on every retrieval

- The `RetrievalResult.provenance` field is non-optional. If you add a
  retrieval path, you must populate provenance — query → VSA match →
  symbolic dereference → source hash → source text.
- Don't strip the source span when refactoring the writer. The span is
  what the reader dereferences back to the original text.

### 4. Benchmark honesty

- Run the full Tier-1 / Tier-4 suite before claiming a recall number in
  a PR description or commit message.
- Use the deterministic judge (`--judge det`) for local runs; reserve
  the Gemini judge for CI — it costs tokens and is rate-limited.
- Two independent graders are required before claiming a fix. The
  deterministic judge grades slightly higher than the LLM judge; both
  must move in the same direction for the claim to stand.
- Never round up. `0.052 recall` is `5.2%`, not `~6%` and not `10%`.
- Document the run ID (e.g. GHA run 33181804451) and SHA in any
  benchmark block you add to `docs/BENCHMARKS.md`.

### 5. PR scope

- One concern per PR. A PR that touches both the reader and the writer
  will be closed with `please-split`.
- Don't refactor unrelated code in a PR that claims a recall fix. The
  diff must be readable end-to-end by a human in under five minutes.
- Don't add dependencies. The core package has one runtime dependency
  (`numpy>=1.24`); optional deps are gated behind `[project.optional-
  dependencies]`. Adding a new one is a governance decision, not an
  implementation detail.

### 6. Worklog protocol

Append (do not overwrite) a new section to `worklog.md` using this
template:

```markdown
---
Task ID: <short, e.g. 20-list-superseded>
Agent: <agent name>
Task: <one-line summary of what was asked>

Work Log:
- <concrete step 1>
- <concrete step 2>
- ...

Stage Summary:
- <key results / important decisions / produced artifacts>
```

The worklog is append-only. If you made a mistake, document it in the
next section — do not edit the previous one.

### 7. When in doubt, don't

If you are about to make a change and you cannot find prior art in
`worklog.md` or `docs/`, stop. Open an issue, describe the change, and
wait for the `accepted` label. The repo rewards patience.

## Worklog template for human contributors

Same template as above (§6). Paste it at the bottom of `worklog.md`
when your PR is ready for review. The reviewer will read the worklog
entry before reading the diff.

## License

By contributing, you agree that your contributions are licensed under
the [Apache-2.0](LICENSE) license that covers this repository.
