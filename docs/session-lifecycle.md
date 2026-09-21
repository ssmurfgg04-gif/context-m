# Session lifecycle — auto-capture wiring (v0.6.8)

Three MCP tools implement the ai-memory-style capture loop on top of
the existing Trace (no new tables, no new daemons):

| Tool | Call on | Does |
|---|---|---|
| `contextm_session_start` | session start | mints `run_id` (if none given), returns briefing block for prompt injection |
| `contextm_session_note` | decisions, lessons, progress | stores under the run (`kind`: observation \| decision \| lesson \| progress) |
| `contextm_session_end` | session end | refreshes TMT summaries, renders a handoff brief the next agent resumes from |

Lessons must follow the Learning Loop form (affirmative, imperative,
keyword-rich) — the tool stores what you give it; retrievability is
on the author.

## OpenCode hook recipes

OpenCode exposes session lifecycle as plugin events (`session.created`,
`session.deleted`, `session.idle`, `tool.execute.after`). A minimal
auto-capture plugin:

- `session.created` → call `contextm_session_start`, inject the
  briefing into context, keep the returned `run_id` for the session.
- `tool.execute.after` (Write/Edit/Bash) → optional: call
  `contextm_session_note` with `kind: "progress"` for significant
  actions only (every tool call is too chatty; gate on file writes
  and completed plans).
- `session.idle` / `session.deleted` → call `contextm_session_end`,
  persist the handoff brief where the next session will find it.

Claude Code equivalent: `.claude/settings.json` hooks
(`SessionStart`, `Stop`) invoking the same three tools.

## Notes

- Session scoping rides the existing `run_id` column — TMT L2
  summaries already group by it, so `session_end` handoffs compose
  with consolidation for free.
- `session_end` on an empty run returns a graceful
  "No observations" brief instead of erroring.
- Server-side `serve_warmup_enabled` (default on) keeps first-recall
  latency down after every (re)spawn.
