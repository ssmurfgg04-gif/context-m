/**
 * context-m-claude — Claude Code extension backed by the Context-M
 * Memory Fabric.
 *
 * The distribution Trojan horse: developers install this plugin and get
 * entity graphs, temporal reasoning, contradiction resolution,
 * cryptographic audit trails, and edge deployment — instead of a flat
 * JSON file of custom instructions.
 *
 * Architecture: a thin MCP client that routes memory operations to the
 * local Context-M MCP server (stdio). Lifecycle:
 *   on session start → recall last working state → "I see you've been working on X. Continue?"
 *   pre-turn         → contextm_search(relevant memories) → inject as context
 *   post-turn        → contextm_add(new conversation facts)
 *   on session end   → "Store summary? [Y/n]" → contextm_add(summary) if yes
 *   on-demand        → contextm_audit / contextm_prove for explainability
 *
 * Auto-load: register as a Claude Code MCP server via the standard
 * .claude/settings.json mcpServers config — Claude picks it up
 * automatically on session start.
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import * as fs from "fs";
import * as path from "path";
import * as os from "os";

export interface MemoryHit {
  id: string;
  memory: string;
  score: number;
  valid_from?: string;
  valid_to?: string | null;
  hash?: string;
}

export interface SessionEndSummary {
  summary: string;
  topic: string;
  changed_files: string[];
  open_questions: string[];
}

const SESSION_STATE_FILE = path.join(
  os.homedir(),
  ".context-m",
  "session_state.json",
);

/**
 * Read the last session state from disk. Used by onSessionStart to
 * greet the user with a "Continue?" prompt based on what they were
 * doing last time.
 */
export function readSessionState(): Record<string, unknown> | null {
  try {
    if (!fs.existsSync(SESSION_STATE_FILE)) return null;
    const raw = fs.readFileSync(SESSION_STATE_FILE, "utf-8");
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

/**
 * Persist the session state to disk. Called by onSessionEnd so the
 * next session can pick up where this one left off.
 */
export function writeSessionState(state: Record<string, unknown>): void {
  try {
    const dir = path.dirname(SESSION_STATE_FILE);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(SESSION_STATE_FILE, JSON.stringify(state, null, 2));
  } catch (err) {
    console.error("[context-m-claude] failed to write session state:", err);
  }
}

export class ContextMClaude {
  private client: Client | null = null;
  private userId: string;
  private sessionStart: Date = new Date();
  private turnCount: number = 0;

  constructor(userId: string = process.env.USER || "developer") {
    this.userId = userId;
  }

  /** Connect to the local Context-M MCP server. */
  async connect(dbPath?: string): Promise<void> {
    const env: Record<string, string> = {
      ...(process.env as Record<string, string>),
    };
    if (dbPath) env.CONTEXT_M_DB = dbPath;
    this.client = new Client({ name: "context-m-claude", version: "0.2.0" });
    const transport = new StdioClientTransport({
      command: "cortexm",
      args: ["serve"],
      env,
    });
    await this.client.connect(transport);
  }

  async disconnect(): Promise<void> {
    await this.client?.close();
    this.client = null;
  }

  /**
   * SESSION START HOOK.
   *
   * Auto-called by Claude Code when a session begins. Reads the last
   * session state and greets the user with a "Continue?" prompt.
   *
   * Returns a greeting string Claude should inject as system context.
   */
  async onSessionStart(): Promise<string> {
    this.sessionStart = new Date();
    this.turnCount = 0;
    await this.connect();
    try {
      const state = readSessionState();
      if (!state) {
        return "[context-m] No prior session found. New memory palace initialized. μ=0 extractor ready.";
      }
      const topic = (state.topic as string) || "unknown work";
      const lastSeen = (state.last_seen as string) || "earlier";
      const factCount = (state.fact_count as number) || 0;
      const lastQuery = (state.last_query as string) || "";
      const openQuestions = (state.open_questions as string[]) || [];
      // also pull the actual last memories from the fabric
      const lastMemories = await this.recall(lastQuery || topic, 5);
      const greeting = `[context-m] I see you've been working on "${topic}" (last seen ${lastSeen}, ${factCount} facts in palace).

Recent memory hits:
${lastMemories || "(none)"}

${openQuestions.length > 0 ? `Open questions from last session:\n${openQuestions.map((q) => `- ${q}`).join("\n")}\n` : ""}Continue?`;
      return greeting;
    } finally {
      await this.disconnect();
    }
  }

  /**
   * SESSION END HOOK.
   *
   * Auto-called by Claude Code when a session ends. Prompts the user:
   * "Store summary? [Y/n]" — if yes, persists a summary fact and the
   * session state to disk for the next session's greeting.
   */
  async onSessionEnd(summary?: SessionEndSummary): Promise<string> {
    if (!summary) {
      // ask the user interactively — Claude Code passes the user's answer
      return "[context-m] Store summary? [Y/n]";
    }
    await this.connect();
    try {
      const durationMin = Math.round(
        (Date.now() - this.sessionStart.getTime()) / 60000,
      );
      // store the summary as a memory fact (μ=0 deterministic)
      const summaryText = `Session summary (${durationMin}m, ${this.turnCount} turns): ${summary.summary}. Topic: ${summary.topic}. Changed: ${summary.changed_files.join(", ")}. Open questions: ${summary.open_questions.join("; ")}`;
      const stored = await this.remember([
        { role: "system", content: summaryText },
      ]);
      // persist session state for next time's greeting
      writeSessionState({
        topic: summary.topic,
        last_seen: new Date().toISOString(),
        fact_count: stored,
        last_query: summary.topic,
        open_questions: summary.open_questions,
        duration_min: durationMin,
        turns: this.turnCount,
      });
      return `[context-m] Stored summary (${stored} facts). See you next session.`;
    } finally {
      await this.disconnect();
    }
  }

  /** Pre-turn hook: retrieve the working set of relevant memories. */
  async recall(query: string, limit = 12): Promise<string> {
    this.turnCount++;
    const res = await this.call("contextm_search", {
      query,
      user_id: this.userId,
      limit,
    });
    return res.content?.[0]?.text ?? "";
  }

  /** Post-turn hook: store new facts from the conversation (μ=0). */
  async remember(
    messages: Array<{ role: string; content: string }>,
  ): Promise<number> {
    const res = await this.call("contextm_add", {
      messages,
      user_id: this.userId,
    });
    try {
      const parsed = JSON.parse(res.content?.[0]?.text ?? "{}");
      return parsed.stored ?? 0;
    } catch {
      return 0;
    }
  }

  /** On-demand: the Why audit trail with hash verification. */
  async audit(query: string): Promise<string> {
    const res = await this.call("contextm_audit", {
      query,
      user_id: this.userId,
    });
    return res.content?.[0]?.text ?? "";
  }

  /** On-demand: ZK-lite proof (content-free attestation for the LLM). */
  async prove(query: string): Promise<string> {
    const res = await this.call("contextm_prove", {
      query,
      user_id: this.userId,
    });
    return res.content?.[0]?.text ?? "";
  }

  /** Temporal queries (Zep-compatible). */
  async temporal(
    op: "before" | "after" | "between",
    start?: string,
    end?: string,
  ): Promise<string> {
    const res = await this.call("contextm_temporal", {
      op,
      start,
      end,
      user_id: this.userId,
    });
    return res.content?.[0]?.text ?? "";
  }

  /**
   * NEW (v0.2): Query-time extraction. Run the deterministic extractor
   * on raw chunks relevant to the query — even if no patterns matched
   * at ingest time. Closes the "half-empty palace" gap.
   */
  async queryExtract(query: string, k = 5): Promise<string> {
    const res = await this.call("contextm_query_extract", {
      query,
      user_id: this.userId,
      k,
    });
    return res.content?.[0]?.text ?? "";
  }

  /**
   * NEW (v0.2): Get attribution for a retrieval result — which source
   * chunks contributed and with what weights (ProtoDash).
   */
  async attribution(query: string): Promise<string> {
    const res = await this.call("contextm_attribution", {
      query,
      user_id: this.userId,
    });
    return res.content?.[0]?.text ?? "";
  }

  async stats(): Promise<string> {
    const res = await this.call("contextm_stats", {});
    return res.content?.[0]?.text ?? "";
  }

  private async call(name: string, args: Record<string, unknown>) {
    if (!this.client) throw new Error("not connected — call connect() first");
    return await this.client.callTool({ name, arguments: args });
  }
}

// ---------------------------------------------------------------------------
// Claude Code hooks wiring — this module exports the turn handlers.
// ---------------------------------------------------------------------------

/**
 * Auto-load entry point. Register this as a Claude Code MCP server
 * in .claude/settings.json:
 *
 *   {
 *     "mcpServers": {
 *       "context-m": {
 *         "command": "cortexm",
 *         "args": ["serve"],
 *         "env": { "CONTEXT_M_DB": "~/.context-m/memory.db" }
 *       }
 *     }
 *   }
 *
 * Claude Code will auto-discover and connect on session start.
 * onSessionStart / onSessionEnd are exposed as Claude Code lifecycle hooks.
 */
export async function onSessionStart(userId?: string): Promise<string> {
  const ctx = new ContextMClaude(userId);
  return await ctx.onSessionStart();
}

export async function onSessionEnd(
  summary: SessionEndSummary,
  userId?: string,
): Promise<string> {
  const ctx = new ContextMClaude(userId);
  return await ctx.onSessionEnd(summary);
}

export async function onUserTurn(
  message: string,
  userId?: string,
): Promise<string> {
  const ctx = new ContextMClaude(userId);
  await ctx.connect();
  try {
    return await ctx.recall(message);
  } finally {
    await ctx.disconnect();
  }
}

export async function onAssistantTurn(
  messages: Array<{ role: string; content: string }>,
  userId?: string,
): Promise<number> {
  const ctx = new ContextMClaude(userId);
  await ctx.connect();
  try {
    return await ctx.remember(messages);
  } finally {
    await ctx.disconnect();
  }
}

// CLI entry point for testing the lifecycle hooks from the command line.
// Usage:
//   node dist/index.js start [userId]      — print session-start greeting
//   node dist/index.js end [userId] [summary] — print session-end prompt
if (process.argv[1] && process.argv[1].endsWith("index.js")) {
  const cmd = process.argv[2];
  const userId = process.argv[3];
  if (cmd === "start") {
    onSessionStart(userId).then((greeting) => console.log(greeting));
  } else if (cmd === "end") {
    const summaryArg = process.argv[4] || "manual test session";
    onSessionEnd(
      {
        summary: summaryArg,
        topic: "context-m development",
        changed_files: [],
        open_questions: [],
      },
      userId,
    ).then((msg) => console.log(msg));
  } else if (cmd === "recall") {
    const query = process.argv[4] || "what was I working on";
    onUserTurn(query, userId).then((result) => console.log(result));
  }
}
