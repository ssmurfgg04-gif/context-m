/**
 * dsh-cortexm — DeepSeek Harness (Cordis) plugin entry.
 *
 * Exposes Context-M's bi-temporal VSA memory + cognition engine +
 * BLAKE3-chained provenance as a DSH storage+session plugin.
 *
 * Architecture (per Cordis spatiotemporal-composability contract):
 *   - On load: spawn `cortexm serve` (stdio JSON-RPC). This is the
 *     same MCP server process we already ship; no new transport.
 *   - ctx.effect() registers cleanup that (a) flushes pending writes,
 *     (b) closes the stdio pipe, (c) leaves NO orphan listeners
 *     ("no orphan listener, no open connection and no ghost command
 *     left behind" — Cordis paper §3.4).
 *   - All plugin methods are thin JSON-RPC forwarders — the heavy
 *     lifting (Trace store, VSA Palace, security stack) lives in
 *     Python. Node-side concerns: subprocess lifecycle, JSON-RPC
 *     framing, hook integration with DSH's tools/pre-execute and
 *     tools/post-execute pipeline.
 *
 * Lean and simple: <300 LoC, zero runtime deps (only `node:` builtins).
 */

import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";

// ---------- JSON-RPC over stdio to `cortexm serve` ----------
class CortexBridge {
  constructor({ db = ":memory:", env = {} } = {}) {
    this.db = db;
    this.env = { ...env };
    this.proc = null;
    this.pending = new Map();           // reqid → {resolve, reject, ts}
    this.buf = "";                       // partial-line accumulator
    this.reqid = 0;
    this._closed = false;
  }

  async start() {
    if (this.proc) return;
    const env = { ...process.env, ...this.env,
                  CORTEXM_DB: this.db };
    this.proc = spawn("cortexm", ["serve"], {
      env,
      stdio: ["pipe", "pipe", "inherit"],
    });
    this.proc.stdout.setEncoding("utf8");
    this.proc.stdout.on("data", (chunk) => this._on_stdout(chunk));
    this.proc.on("exit", (code, sig) => {
      if (!this._closed) {
        const err = new Error(`cortexm serve exited (code=${code} sig=${sig})`);
        for (const { reject } of this.pending.values()) reject(err);
        this.pending.clear();
      }
    });
    // Wait for initialize handshake
    await this._rpc("initialize", {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: { name: "dsh-cortexm", version: "0.1.0" },
    });
  }

  _on_stdout(chunk) {
    this.buf += chunk;
    let idx;
    while ((idx = this.buf.indexOf("\n")) >= 0) {
      const line = this.buf.slice(0, idx);
      this.buf = this.buf.slice(idx + 1);
      if (!line.trim()) continue;
      let msg;
      try { msg = JSON.parse(line); } catch { continue; }
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(msg.error.message || "rpc error"));
        else resolve(msg.result);
      }
      // notifications are dropped — dsh-cortexm is a storage/session
      // plugin, not a tools plugin, so progress notifications are
      // not relevant to the DSH pipeline.
    }
  }

  _rpc(method, params) {
    return new Promise((resolve, reject) => {
      if (!this.proc) { reject(new Error("bridge not started")); return; }
      const id = ++this.reqid;
      this.pending.set(id, { resolve, reject, ts: Date.now() });
      const frame = JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n";
      this.proc.stdin.write(frame);
      // 30s timeout — DSH tool pipeline expects storage calls to be fast.
      // The μ=0 reader path is sub-millisecond; 30s is generous for
      // consolidate (which can run cognition).
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`rpc timeout: ${method}`));
        }
      }, 30_000).unref();
    });
  }

  /** Public plugin API — mirrors cortexm MCP tools. */
  async add({ user_id, agent_id, run_id, text, created_at }) {
    return this._rpc("tools/call", {
      name: "contextm_add",
      arguments: { user_id, agent_id, run_id, text, created_at },
    });
  }

  async search({ user_id, query, agent_id, run_id, k }) {
    return this._rpc("tools/call", {
      name: "contextm_search",
      arguments: { user_id, query, agent_id, run_id, k },
    });
  }

  async structural_query({ user_id, subject, relation, valid_at }) {
    return this._rpc("tools/call", {
      name: "contextm_structural_query",
      arguments: { user_id, subject, relation, valid_at },
    });
  }

  async consolidate({ user_id }) {
    return this._rpc("tools/call", {
      name: "contextm_consolidate",
      arguments: { user_id },
    });
  }

  async export_provenance({ user_id, format }) {
    return this._rpc("tools/call", {
      name: "contextm_export_provenance",
      arguments: { user_id, format },
    });
  }

  async audit({ user_id, n }) {
    return this._rpc("tools/call", {
      name: "contextm_audit",
      arguments: { user_id, n },
    });
  }

  async inspect({ user_id, agent_id, run_id, what, limit }) {
    return this._rpc("tools/call", {
      name: "contextm_inspect",
      arguments: { user_id, agent_id, run_id, what, limit },
    });
  }

  // session-replay primitives (Reddit deep-dive 2026-08-29: ≥10
  // mentions of "replay" / "trajectory view" / "session log")
  // The audit log already has every event; replay is just
  // re-emitting them in order. Fork = copy up to tx-id + continue.
  async replay({ user_id, from_ts, to_ts }) {
    const audit = await this.audit({ user_id, n: 10_000 });
    return { user_id, events: audit.events.filter(e =>
      (!from_ts || e.ts >= from_ts) && (!to_ts || e.ts <= to_ts)) };
  }

  async fork({ user_id, at_tx_id }) {
    // DSH session fork = copy audit log up to at_tx_id, then continue
    // with a new run_id derived from at_tx_id.
    const audit = await this.audit({ user_id, n: 10_000 });
    const cutoff = audit.events.findIndex(e => e.id === at_tx_id);
    if (cutoff < 0) {
      const err = new Error(`fork point ${at_tx_id} not in audit log`);
      err.code = "FORK_POINT_NOT_FOUND";
      throw err;
    }
    return {
      forked_run_id: `${at_tx_id.slice(0, 8)}-fork-${randomUUID().slice(0, 8)}`,
      events: audit.events.slice(0, cutoff + 1),
    };
  }

  async trajectory({ user_id, n = 200 }) {
    // Reddit "trajectory view" ask — visualizable event stream
    const audit = await this.audit({ user_id, n });
    return {
      user_id,
      n_events: audit.events.length,
      events: audit.events.map((e, i) => ({
        step: i,
        id: e.id,
        ts: e.ts,
        kind: e.kind,
        payload_summary: e.payload_summary,
      })),
    };
  }

  async close() {
    this._closed = true;
    if (!this.proc) return;
    try { this.proc.stdin.end(); } catch {}
    try { await new Promise(r => this.proc.on("exit", r)); } catch {}
    try { this.proc.kill("SIGKILL"); } catch {}
    this.proc = null;
  }
}

// ---------- DSH Plugin Export ----------
// Cordis plugin contract: default export is an object with a
// `register(ctx)` function that mounts the plugin's hooks via
// ctx.effect() and returns the plugin's public API.
//
// ctx.effect(register, cleanup) — Cordis spatiotemporal-composability
// primitive. On unload, cleanup runs and ALL side effects disappear
// ("no orphan listener, no open connection and no ghost command left
// behind" — Cordis paper §3.4).
//
// We expose BOTH a storage plugin AND a session plugin:
//   - storage: add/search/structural_query/consolidate/export_provenance/audit
//   - session: replay/fork/trajectory/inspect (Reddit ≥10-mention asks)
//
// DSH's tools/pre-execute hook is the right place to mount MINJA
// pattern scan + MIND diversity check on retrieved context before
// it reaches the LLM. tools/post-execute is where PII redaction on
// tool results goes. These are future-work hooks; for now we expose
// the raw memory primitives and let upstream DSH plugins compose.

/**
 * @typedef {Object} CordisCtx
 * @property {function} effect - register a (setup, cleanup) pair
 * @property {function} inject - dependency injection
 * @property {Object} config - plugin config (CORTEXM_DB, etc.)
 * @property {Object} log - structured logger
 */

export default {
  name: "cortexm",
  kind: ["storage", "session"],

  /**
   * Register the plugin.
   * @param {CordisCtx} ctx
   */
  async register(ctx) {
    const cfg = ctx.config || {};
    const bridge = new CortexBridge({
      db: cfg.CORTEXM_DB || process.env.CORTEXM_DB || ":memory:",
      env: {
        CORTEXM_CODEC: cfg.CORTEXM_CODEC || "int8",
        CORTEXM_FADE: cfg.CORTEXM_FADE || "exponential",
        CORTEXM_COGNITION: String(cfg.CORTEXM_COGNITION ?? true),
        CORTEXM_PROVENANCE: String(cfg.CORTEXM_PROVENANCE ?? true),
        CORTEXM_CHUNK_RECALL_USE_BM25:
          String(cfg.CORTEXM_CHUNK_RECALL_USE_BM25 ?? true),
      },
    });

    await bridge.start();

    // ctx.effect(register_fn, cleanup_fn) — Cordis composability
    // primitive. On plugin unload, the cleanup_fn runs and the
    // subprocess disappears completely.
    if (typeof ctx.effect === "function") {
      ctx.effect(
        () => {
          // register side effect: storage/session hooks
          // (no global listeners to register — all access goes
          // through the returned bridge object)
        },
        async () => {
          await bridge.close();
        }
      );
    }

    // The plugin's public API — DSH agents access these via
    // ctx.storage.cortexm.* and ctx.session.cortexm.*
    return {
      storage: {
        add: (args) => bridge.add(args),
        search: (args) => bridge.search(args),
        structural_query: (args) => bridge.structural_query(args),
        consolidate: (args) => bridge.consolidate(args),
        export_provenance: (args) => bridge.export_provenance(args),
        audit: (args) => bridge.audit(args),
      },
      session: {
        replay: (args) => bridge.replay(args),
        fork: (args) => bridge.fork(args),
        trajectory: (args) => bridge.trajectory(args),
        inspect: (args) => bridge.inspect(args),
      },
      // direct bridge access for power users (DSH Creator mode)
      _bridge: bridge,
    };
  },
};
