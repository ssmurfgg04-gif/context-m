# opencode-auto-memory

Deterministic context-m capture for [opencode](https://opencode.ai) — memory
that records itself, with **zero LLM cost**.

context-m never records anything by itself; it only stores what is explicitly
passed to it. This plugin closes that gap from inside the agent harness:

| Trigger | Stored as | Cost |
|---|---|---|
| `git commit -m "…"` (bash tool) | `session-note --kind progress` → `shipped commit: …` | $0 |
| Completed todo (todowrite) | `session-note --kind progress` → `completed: …` | $0 |
| Answered question (question tool) | `session-note --kind decision` → `Q → A` | $0 |
| Session idle | `cortexm consolidate` (local passes only) | $0 |

Every fact is paired with one durable repo lesson (deduped per session).
All failures are swallowed — memory must never break a session.

## Install

1. Copy `auto-memory.ts` into your opencode plugin dir:
   - Project: `.opencode/plugins/auto-memory.ts`
   - Global: `~/.config/opencode/plugins/auto-memory.ts`
2. Requirements: `cortexm` on `PATH`, `PYTHONUTF8=1` (the plugin sets it
   for its child processes itself).
3. Database: `CONTEXT_M_DB` env, else `<session-directory>/data/context-m.db`.
4. **Restart opencode** — plugins load at startup.

## Verify

- Make any `git commit`, then check `.opencode/.auto-memory/journal.jsonl`
  for the `shipped commit:` entry.
- Read it back: `cortexm inspect --user-id <you> --what chunks --limit 5`.

## Design notes

- No model calls anywhere in the loop: commit detection is a regex,
  todo/decision capture reads hook payloads, consolidation is local.
- Hook payload shapes differ by stage, so args are read from both
  `input.args` and `input.output.args`.
- `tauri::test`-style heavy harnesses are irrelevant here; the only
  external dependency is the `cortexm` CLI.
- Unknown hook shapes are ignored, never thrown on.

## License

Same as the context-m repository root.
