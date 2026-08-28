// Shared LLM plumbing for Context-M benchmark tooling.
//
// Design goals:
//   - deterministic caching (SHA-256 keyed by backend+model+payload) so
//     re-runs never re-bill and cross-backend results never mix
//   - bounded concurrency with retries + exponential backoff
//   - strict-JSON extraction from chatty models
//   - honest accounting: model id + token usage recorded per call
//
// Backends (select with LLM_BACKEND, default "zai"):
//   zai    — z-ai-web-dev-sdk gateway (GLM models)
//   gemini — Google Generative Language API with an API key
//            (GEMINI_API_KEY, model via LLM_MODEL,
//             default gemini-3.5-flash-lite)
//
// Env knobs:
//   LLM_BACKEND    "zai" | "gemini"
//   GEMINI_API_KEY Google API key (required for the gemini backend)
//   LLM_MODEL      model override (e.g. gemini-3.5-flash, gpt-5-judge-alias)
//   ZAI_SDK_PATH   absolute path to z-ai-web-dev-sdk dist/index.js
//   LLM_CACHE_DIR  cache directory (default: ./benchmarks/llm/.cache)
//   LLM_CONCURRENCY  parallel in-flight requests (default 6)
//
// NOTE on region availability: the Gemini API refuses requests from
// unsupported regions (HTTP 400 FAILED_PRECONDITION "User location is not
// supported"). When that happens we fail fast with an actionable message
// instead of burning retries — run from a supported region or use the
// provided GitHub Actions workflow (.github/workflows/llm-eval.yml).

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

const DEFAULT_SDK =
  "/home/z/.bun/install/global/node_modules/z-ai-web-dev-sdk/dist/index.js";
const SDK_PATH = process.env.ZAI_SDK_PATH || DEFAULT_SDK;

export const BACKEND = process.env.LLM_BACKEND || "zai";
export const GEMINI_MODEL = process.env.LLM_MODEL || "gemini-3.5-flash-lite";
const GEMINI_KEY = process.env.GEMINI_API_KEY || "";
const GEMINI_BASE =
  process.env.GEMINI_BASE || "https://generativelanguage.googleapis.com/v1beta";

// ---------------------------------------------------------------- zai backend

let _client = null;
async function zaiClient() {
  if (!_client) {
    const { default: ZAI } = await import(SDK_PATH);
    _client = await ZAI.create();
  }
  return _client;
}

async function chatZai(messages, temperature, maxRetries) {
  const zai = await zaiClient();
  let lastErr = null;
  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      const completion = await zai.chat.completions.create({
        messages,
        thinking: { type: "disabled" },
        temperature,
      });
      return {
        content: completion.choices?.[0]?.message?.content ?? "",
        model: completion.model ?? "unknown",
        usage: completion.usage ?? null,
      };
    } catch (e) {
      lastErr = e;
      const msg = String(e?.message || e);
      const isRate = msg.includes("429") || msg.toLowerCase().includes("too many");
      if (attempt >= maxRetries) break;
      if (isRate) {
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

// -------------------------------------------------------------- gemini backend

class RegionBlockedError extends Error {
  constructor(msg) { super(msg); this.name = "RegionBlockedError"; }
}

// Map OpenAI-style messages to Gemini contents + systemInstruction.
function geminiPayload(messages, temperature) {
  const system = messages
    .filter(m => m.role === "system")
    .map(m => m.content)
    .join("\n\n");
  const contents = messages
    .filter(m => m.role !== "system")
    .map(m => ({
      role: m.role === "assistant" ? "model" : "user",
      parts: [{ text: m.content }],
    }));
  const out = { contents, generationConfig: { temperature } };
  if (system) out.systemInstruction = { parts: [{ text: system }] };
  return out;
}

async function chatGemini(messages, temperature, maxRetries, tag) {
  if (!GEMINI_KEY) {
    throw new Error("gemini backend selected but GEMINI_API_KEY is not set");
  }
  const url = `${GEMINI_BASE}/models/${GEMINI_MODEL}:generateContent`;
  const body = JSON.stringify(geminiPayload(messages, temperature));
  let lastErr = null;
  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    let res;
    try {
      res = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-goog-api-key": GEMINI_KEY,
        },
        body,
      });
    } catch (e) {
      lastErr = e;
      if (attempt >= maxRetries) break;
      await new Promise(r => setTimeout(r, 1000 * attempt * attempt));
      continue;
    }
    if (res.ok) {
      const d = await res.json();
      const parts = d?.candidates?.[0]?.content?.parts ?? [];
      const content = parts.map(p => p.text || "").join("");
      const um = d?.usageMetadata ?? null;
      return {
        content,
        model: d?.modelVersion ? `gemini:${d.modelVersion}` : `gemini:${GEMINI_MODEL}`,
        usage: um ? {
          prompt_tokens: um.promptTokenCount ?? null,
          completion_tokens: um.candidatesTokenCount ?? null,
          total_tokens: um.totalTokenCount ?? null,
        } : null,
      };
    }
    const errText = await res.text().catch(() => "");
    // Region block: deterministic, retries cannot help — fail with guidance.
    if (res.status === 400 && /location is not supported/i.test(errText)) {
      throw new RegionBlockedError(
        `Gemini API refused the request: this egress region is not supported ` +
        `(${GEMINI_BASE}). Run from a supported region or use ` +
        `.github/workflows/llm-eval.yml (GitHub-hosted runners). ` +
        `Backend detail: ${errText.slice(0, 200)}`);
    }
    lastErr = new Error(`gemini HTTP ${res.status}: ${errText.slice(0, 300)}`);
    const isRate = res.status === 429 || res.status >= 500;
    if (attempt >= maxRetries) break;
    if (isRate) {
      const waitMs = Math.min(180_000, 15_000 * Math.pow(2, attempt - 1));
      await new Promise(r => setTimeout(r, waitMs));
    } else if (attempt < 3) {
      await new Promise(r => setTimeout(r, 800 * attempt * attempt));
    } else {
      break;
    }
  }
  throw new Error(`LLM call failed after ${maxRetries} retries: ${lastErr?.message}`);
}

// ------------------------------------------------------------------ dispatch

export function cacheDir() {
  const d = process.env.LLM_CACHE_DIR ||
    path.join(process.cwd(), "benchmarks", "llm", ".cache");
  fs.mkdirSync(d, { recursive: true });
  return d;
}

function keyOf(tag, payload) {
  // Backend + model are part of the key: a cached GLM answer must never be
  // replayed as a Gemini answer and vice versa.
  const h = crypto.createHash("sha256")
    .update(`${tag}\u0000${BACKEND}\u0000${GEMINI_MODEL}\u0000` +
            JSON.stringify(payload))
    .digest("hex");
  return h.slice(0, 40);
}

/** Raw chat completion with retry + cache. Returns {content, usage, model, cached}. */
export async function chat(messages, opts = {}) {
  const tag = opts.tag || "llm";
  const temperature = opts.temperature ?? 0.7;
  const maxRetries = opts.retries ?? 6;
  const payload = { messages, temperature };
  const key = keyOf(tag, payload);
  const cacheFile = path.join(cacheDir(), key + ".json");
  if (fs.existsSync(cacheFile)) {
    return { ...JSON.parse(fs.readFileSync(cacheFile, "utf8")), cached: true };
  }
  const out = BACKEND === "gemini"
    ? await chatGemini(messages, temperature, maxRetries, tag)
    : await chatZai(messages, temperature, maxRetries);
  fs.writeFileSync(cacheFile, JSON.stringify(out));
  return { ...out, cached: false };
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

/** Human-readable backend identity for results files ("judge identity"). */
export function backendIdentity() {
  return BACKEND === "gemini"
    ? `google:${GEMINI_MODEL} (generativelanguage api)`
    : `zai-gateway (GLM, default model)`;
}

export const JUDGE_MODEL_PLACEHOLDER = "recorded-per-run";
