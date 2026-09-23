/**
 * auto-memory — deterministic context-m capture for opencode.
 *
 * WHY: context-m never records anything by itself; it only stores what is
 * explicitly passed to it. This plugin closes that gap with zero LLM cost:
 *
 *   tool.execute.after (bash git commit)  -> session-note --kind progress
 *   tool.execute.after (todowrite done)   -> session-note --kind progress
 *   tool.execute.after (question answered)-> session-note --kind decision
 *   session.idle                          -> cortexm consolidate (local only)
 *
 * Every fact is paired with one durable repo lesson (LAW). Lessons are
 * de-duplicated per plugin lifetime so the store is not spammed.
 * All failures are swallowed (debug-logged) — memory must never break a session.
 *
 * DB: CONTEXT_M_DB env, else <session-directory>/data/context-m.db
 * Requires: cortexm on PATH, PYTHONUTF8=1 (set below for the child).
 */

import { execFile } from "node:child_process";
import { appendFile, mkdir } from "node:fs/promises";
import { join } from "node:path";

const DB_FALLBACK = "data/context-m.db";
const JOURNAL_DIR = ".opencode/.auto-memory";
const JOURNAL_FILE = "journal.jsonl";

// Durable repo lessons (affirmative, imperative, keyword-rich).
const LESSONS = {
  commit:
    "Commit per completed task with conventional messages because the git log plus the ledger form the recovery map after compaction.",
  todo:
    "Mark a todo complete only after its verification output exists, never on intent.",
  decision:
    "Record user decisions verbatim with flags and order because re-asking answered questions wastes the session.",
} as const;

type LessonKey = keyof typeof LESSONS;

interface Ctx {
  directory: string;
  client: { app: { log: (e: unknown) => Promise<void> } };
}

const seenLessons = new Set<string>();

function dbPath(directory: string): string {
  const fromEnv = process.env.CONTEXT_M_DB;
  if (fromEnv && fromEnv.trim()) return fromEnv;
  return join(directory, DB_FALLBACK);
}

function runCortexm(args: string[], cwd: string): Promise<{ ok: boolean; out: string }> {
  return new Promise((resolve) => {
    execFile(
      "cortexm",
      args,
      {
        cwd,
        timeout: 30_000,
        env: { ...process.env, PYTHONUTF8: "1" },
        windowsHide: true,
      },
      (err, stdout, stderr) => {
        if (err) resolve({ ok: false, out: String(stderr || err.message).slice(0, 500) });
        else resolve({ ok: true, out: String(stdout).slice(0, 500) });
      }
    );
  });
}

async function journal(ctx: Ctx, entry: Record<string, unknown>): Promise<void> {
  try {
    await mkdir(join(ctx.directory, JOURNAL_DIR), { recursive: true });
    await appendFile(
      join(ctx.directory, JOURNAL_DIR, JOURNAL_FILE),
      JSON.stringify({ ts: new Date().toISOString(), ...entry }) + "\n",
      "utf8"
    );
  } catch {
    /* journal is best-effort */
  }
}

async function note(
  ctx: Ctx,
  kind: "observation" | "decision" | "lesson" | "progress",
  text: string,
  lessonKey?: LessonKey
): Promise<void> {
  const clean = text.replace(/\s+/g, " ").trim().slice(0, 500);
  if (!clean) return;
  const db = dbPath(ctx.directory);
  try {
    await runCortexm(
      ["session-note", "--db", db, "--user-id", "jack", "--kind", kind, clean],
      ctx.directory
    );
    await journal(ctx, { kind, text: clean });
    if (lessonKey && !seenLessons.has(lessonKey)) {
      seenLessons.add(lessonKey);
      await runCortexm(
        ["session-note", "--db", db, "--user-id", "jack", "--kind", "lesson", LESSONS[lessonKey]],
        ctx.directory
      );
      await journal(ctx, { kind: "lesson", text: LESSONS[lessonKey] });
    }
  } catch (err) {
    try {
      await ctx.client.app.log({
        body: { service: "auto-memory", level: "debug", message: `note failed: ${String(err).slice(0, 200)}` },
      });
    } catch {
      /* never break the session */
    }
  }
}

function extractCommitMessage(command: string): string | null {
  // Matches: git commit -m "msg" / git commit -m 'msg' (also via git -C ... commit)
  const m = command.match(/git\b[^;|&]*\bcommit\b[^;|&]*?-m\s+("([^"]+)"|'([^']+)'|(\S+))/);
  if (!m) return null;
  return (m[2] ?? m[3] ?? m[4] ?? "").trim() || null;
}

export const AutoMemoryPlugin = async (ctx: Ctx) => {
  // Per-process todo state to detect newly-completed items.
  const doneBefore = new Set<string>();

  return {
    "tool.execute.after": async (input: any) => {
      try {
        const tool = (input?.tool ?? input?.output?.tool) as string | undefined;
        // Args may live on input or output depending on hook stage; check both.
        const args = ({
          ...((input?.output?.args ?? {}) as Record<string, any>),
          ...((input?.args ?? {}) as Record<string, any>),
        }) as Record<string, any>;

        // 1. git commits -> progress note
        if (tool === "bash" && typeof args.command === "string") {
          const msg = extractCommitMessage(args.command);
          if (msg) {
            await note(ctx, "progress", `shipped commit: ${msg}`, "commit");
          }
          return;
        }

        // 2. newly completed todos -> progress note
        if (tool === "todowrite" && Array.isArray(args.todos)) {
          for (const t of args.todos) {
            const content = String(t?.content ?? "").trim();
            if (t?.status === "completed" && content && !doneBefore.has(content)) {
              doneBefore.add(content);
              await note(ctx, "progress", `completed: ${content}`, "todo");
            }
          }
          return;
        }

        // 3. answered questions -> decision note
        if (tool === "question") {
          const qs = Array.isArray(args.questions) ? args.questions : [];
          const out = (input?.output ?? {}) as any;
          const rawAnswers = Array.isArray(out)
            ? out
            : Array.isArray(out?.answers)
              ? out.answers
              : [];
          const answers: string[] = rawAnswers.map((a: any) =>
            typeof a === "string" ? a : String(a?.label ?? a?.value ?? a ?? "")
          );
          qs.forEach((q: any, i: number) => {
            const question = String(q?.question ?? "").trim().slice(0, 200);
            const answer = (answers[i] ?? "").trim().slice(0, 200);
            if (question && answer) {
              void note(ctx, "decision", `${question} -> ${answer}`, "decision");
            }
          });
          return;
        }
      } catch {
        /* never break the session */
      }
    },

    // 4. session idle -> consolidate (local passes only, no LLM calls)
    event: async ({ event }: any) => {
      try {
        if (event?.type === "session.idle") {
          const db = dbPath(ctx.directory);
          await runCortexm(["consolidate", "--db", db, "--user-id", "jack"], ctx.directory);
          await journal(ctx, { kind: "consolidate", text: "idle consolidate ran" });
        }
      } catch {
        /* never break the session */
      }
    },
  };
};
