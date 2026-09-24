// Guards on worker/wrangler.toml: no request logs, and the rate-limit tiers we rely on.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const toml = readFileSync(new URL("../wrangler.toml", import.meta.url), "utf8");

// Tiny reader for the flat TOML we write: top-level keys, [tables] and [[arrays of tables]].
function sections(text) {
  const out = { "": {} };
  let cur = out[""];
  for (const raw of text.split("\n")) {
    const line = raw.replace(/#.*$/, "").trim();
    if (!line) continue;
    const arr = line.match(/^\[\[(.+)\]\]$/);
    const tbl = line.match(/^\[(.+)\]$/);
    if (arr) { (out[arr[1]] ||= []).push(cur = {}); continue; }
    if (tbl) { out[tbl[1]] ||= {}; cur = out[tbl[1]]; continue; }
    const kv = line.match(/^([A-Za-z_][\w.-]*)\s*=\s*(.+)$/);
    if (kv) cur[kv[1]] = kv[2].replace(/^"(.*)"$/, "$1");
  }
  return out;
}
const cfg = sections(toml);

test("wrangler.toml turns Workers Logs and Logpush off", () => {
  assert.equal(cfg.observability?.enabled, "false", "[observability] enabled must be false: request logs would record who applied");
  assert.equal(cfg[""].logpush, "false");
  assert.match(toml, /^\[observability\]\s*\n\s*enabled\s*=\s*false/m);
});

test("wrangler.toml defines the two rate-limit tiers the Worker fails closed without", () => {
  const rate = cfg["unsafe.bindings"].find((b) => b.name === "RATE");
  const ai = cfg["unsafe.bindings"].find((b) => b.name === "RATE_AI");
  assert.equal(rate?.type, "ratelimit");
  assert.equal(ai?.type, "ratelimit");
  assert.notEqual(rate.namespace_id, ai.namespace_id);
  assert.match(rate.simple, /limit\s*=\s*30\b/);
  assert.match(rate.simple, /period\s*=\s*60\b/);
  assert.match(ai.simple, /limit\s*=\s*10\b/);
  assert.match(ai.simple, /period\s*=\s*60\b/);
  assert.match(toml, /PREVIEW_TOKEN/, "the preview secret is documented next to the others");
});
