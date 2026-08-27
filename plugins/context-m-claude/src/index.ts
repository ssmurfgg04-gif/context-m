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
 * local Context-M MCP server (stdio). Every Claude Code turn:
 *   pre-turn  → contextm_search(relevant memories) → inject as context
 *   post-turn → contextm_add(new conversation facts)
 *   on-demand → contextm_audit / contextm_prove for explainability
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

export interface MemoryHit {
  id: string;
  memory: string;
  score: number;
  valid_from?: string;
  valid_to?: string | null;
  hash?: string;
}

export class ContextMClaude {
  private client: Client | null = null;
  private userId: string;

  constructor(userId: string = process.env.USER || "developer") {
    this.userId = userId;
  }

  /** Connect to the local Context-M MCP server. */
  async connect(dbPath?: string): Promise<void> {
    const env: Record<string, string> = {
      ...(process.env as Record<string, string>),
    };
    if (dbPath) env.CONTEXT_M_DB = dbPath;
    this.client = new Client({ name: "context-m-claude", version: "0.1.0" });
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

  /** Pre-turn hook: retrieve the working set of relevant memories. */
  async recall(query: string, limit = 12): Promise<string> {
    const res = await this.call("contextm_search", {
      query,
      user_id: this.userId,
      limit,
    });
    return res.content?.[0]?.text ?? "";
  }

  /** Post-turn hook: store new facts from the conversation (μ=0). */
  async remember(messages: Array<{ role: string; content: string }>): Promise<number> {
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
