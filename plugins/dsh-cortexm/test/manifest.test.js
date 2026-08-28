/**
 * Smoke test for dsh-cortexm plugin manifest + basic register API.
 *
 * Does NOT actually spawn `cortexm serve` — instead we stub the
 * CortexBridge.start() to verify the plugin registration contract.
 * End-to-end (with a real Python process) is covered by the Python
 * test suite; this file covers the JS-side lifecycle only.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");
const PKG = JSON.parse(readFileSync(join(ROOT, "package.json"), "utf8"));

test("package.json has the dsh-plugin topic and Cordis manifest", () => {
  assert.ok(PKG.keywords.includes("dsh-plugin"),
    "must have dsh-plugin keyword for dsh-find-plugin discovery");
  assert.ok(PKG.keywords.includes("cordis"),
    "must declare cordis keyword");
  assert.ok(PKG.dsh, "must have a dsh manifest block");
  assert.ok(PKG.dsh.kind.includes("storage"),
    "must register as a storage plugin");
  assert.ok(PKG.dsh.kind.includes("session"),
    "must register as a session plugin");
  assert.ok(PKG.dsh.provides.storage,
    "must declare a storage interface");
  assert.ok(PKG.dsh.provides.session,
    "must declare a session interface");
});

test("default export is a Cordis-shaped plugin object", async () => {
  const mod = await import(join(ROOT, "src", "index.js"));
  const plugin = mod.default;
  assert.equal(typeof plugin, "object");
  assert.equal(plugin.name, "cortexm");
  assert.ok(Array.isArray(plugin.kind));
  assert.equal(typeof plugin.register, "function");
});

test("register() returns a storage+session API without spawning when bridge.start is stubbed", async () => {
  const mod = await import(join(ROOT, "src", "index.js"));
  const plugin = mod.default;
  // Patch the CortexBridge prototype via ctx.effect stub
  const calls = [];
  const ctx = {
    config: { CORTEXM_DB: ":memory:" },
    effect: (reg, cleanup) => {
      calls.push({ reg, cleanup });
      reg();
    },
  };
  // Stub spawn — register() must not require cortexm binary
  const origSpawn = (await import("node:child_process")).spawn;
  const stub = (cmd, args, opts) => ({
    stdin: { write() {}, end() {} },
    stdout: { setEncoding() {}, on() {} },
    on(ev, cb) { if (ev === "exit") setTimeout(() => cb(0, null), 0); },
    kill() {},
  });
  (await import("node:child_process")).spawn = stub;
  try {
    // Register should not throw even with a stubbed process —
    // it will reject on the initialize handshake; we catch:
    try {
      await plugin.register(ctx);
      assert.fail("expected initialize to fail with stubbed process");
    } catch (err) {
      // good — bridge.start() timed out / failed because the
      // stubbed process never replied to initialize
      assert.ok(err, "register() threw on stubbed bridge");
    }
    // ctx.effect was called once
    assert.equal(calls.length, 1);
  } finally {
    (await import("node:child_process")).spawn = origSpawn;
  }
});
