// Shared LLM plumbing for Context-M benchmark tooling.
//
// Design goals:
//   - deterministic caching (SHA-256 keyed) so re-runs never re-bill
//   - bounded concurrency with retries + exponential backoff
//   - strict-JSON extraction from chatty models
//   - honest accounting: model id + token usage recorded per call
//
// Env knobs:
//   ZAI_SDK_PATH   absolute path to z-ai-web-dev-sdk dist/index.js
//   LLM_CACHE_DIR  cache directory (default: ./benchmarks/llm/.cache)
//   LLM_CONCURRENCY  parallel in-flight requests (default 6)

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

const DEFAULT_SDK =
  "/home/z/.bun/install/global/node_modules/z-ai-web-dev-sdk/dist/index.js";
const SDK_PATH = process.env.ZAI_SDK_PATH || DEFAULT_SDK;

let _client = null;
export async function client() {
  if (!_client) {
    const { default: ZAI } = await import(SDK_PATH);
    _client = await ZAI.create();
  }
  return _client;
}

export function cacheDir() {
  const d = process.env.LLM_CACHE_DIR ||
    path.join(process.cwd(), "benchmarks", "llm", ".cache");
  fs.mkdirSync(d, { recursive: true });
  return d;
}

function keyOf(tag, payload) {
  const h = crypto.createHash("sha256")
    .update(tag + "\u0000" + JSON.stringify(payload))
    .digest("hex");
  return h.slice(0, 40);
}

/** Raw chat completion with retry + cache. Returns {content, usage, model, cached}. */
export async function chat(messages, opts = {}) {
  const tag = opts.tag || "llm";
  const payload = { messages, temperature: opts.temperature ?? 0.7 };
  const key = keyOf(tag, payload);
  const cacheFile = path.join(cacheDir(), key + ".json");
  if (fs.existsSync(cacheFile)) {
    return { ...JSON.parse(fs.readFileSync(cacheFile, "utf8")), cached: true };
  }
  const zai = await client();
  const maxRetries = opts.retries ?? 6;
  let lastErr = null;
  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      const completion = await zai.chat.completions.create({
        messages,
        thinking: { type: "disabled" },
        temperature: payload.temperature,
      });
      const content = completion.choices?.[0]?.message?.content ?? "";
      const out = {
        content,
        model: completion.model ?? "unknown",
        usage: completion.usage ?? null,
        cached: false,
      };
      fs.writeFileSync(cacheFile, JSON.stringify(out));
      return out;
    } catch (e) {
      lastErr = e;
      const msg = String(e?.message || e);
      const isRate = msg.includes("429") || msg.toLowerCase().includes("too many");
      if (attempt >= maxRetries) break;
      if (isRate) {
        // rate limited: long exponential backoff, honour the cooldown
        const waitMs = Math.min(180_000, 15_000 * Math.pow(2, attempt - 1));
        await new Promise(r => setTimeout(r, waitMs));
      } else if (attempt < 3) {
        await new Promise(r => setTimeout(r, 800 * attempt * attempt));
      } else {
        break; // non-rate errors: do not burn retries
      }
    }
  }
  throw new Error(`LLM call failed after ${maxRetries} retries: ${lastErr?.message}`);
}

/** Extract the first JSON value from a (possibly fenced) model reply. */
export function parseJSON(text) {
  if (!text) return null;
  let t = text.trim();
  const fence = t.match(/```(?:json)?\s*([\s\S]*?)```/);
  if (fence) t = fence[1].trim();
  try {
    return JSON.parse(t);
  } catch {
    const first = t.indexOf("{");
    const last = t.lastIndexOf("}");
    if (first >= 0 && last > first) {
      try { return JSON.parse(t.slice(first, last + 1)); } catch { /* fallthrough */ }
    }
    const fa = t.indexOf("[");
    const la = t.lastIndexOf("]");
    if (fa >= 0 && la > fa) {
      try { return JSON.parse(t.slice(fa, la + 1)); } catch { /* fallthrough */ }
    }
    return null;
  }
}

/** Chat call that must return JSON; retries on parse failure with a nudge. */
export async function chatJSON(messages, opts = {}) {
  const first = await chat(messages, opts);
  let parsed = parseJSON(first.content);
  if (parsed !== null) return { ...first, json: parsed };
  const nudge = await chat(
    [...messages,
     { role: "user", content: "Your previous reply was not valid JSON. Reply AGAIN with ONLY the JSON value, no prose, no code fences." }],
    { ...opts, temperature: 0.0, tag: (opts.tag || "llm") + ":json-retry" });
  parsed = parseJSON(nudge.content);
  if (parsed !== null) return { ...nudge, json: parsed };
  throw new Error("model refused to emit JSON twice");
}

/** Run fn over items with bounded concurrency; preserves input order. */
export async function mapLimit(items, limit, fn) {
  const out = new Array(items.length);
  let next = 0;
  const workers = Array.from({ length: Math.max(1, Math.min(limit, items.length)) },
    async () => {
      while (true) {
        const i = next++;
        if (i >= items.length) return;
        out[i] = await fn(items[i], i);
      }
    });
  await Promise.all(workers);
  return out;
}

export function readJSONL(p) {
  return fs.readFileSync(p, "utf8").split("\n").filter(l => l.trim())
    .map(l => JSON.parse(l));
}

export function writeJSONL(p, rows) {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, rows.map(r => JSON.stringify(r)).join("\n") + "\n");
}

export const JUDGE_MODEL_PLACEHOLDER = "recorded-per-run";
