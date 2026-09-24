import { test } from "node:test";
import assert from "node:assert/strict";
import { handleTts } from "../src/tts.js";

const post = (body) => new Request("https://api.test/api/tts", { method: "POST", body: JSON.stringify(body) });

test("tts proxies to OpenAI with the Spanish voice and streams audio back", async () => {
  let seen;
  const fakeFetch = async (url, opts) => { seen = { url, body: JSON.parse(opts.body), auth: opts.headers.authorization, signal: opts.signal }; return new Response(new Uint8Array([1, 2, 3]), { status: 200 }); };
  const r = await handleTts(post({ lang: "es", text: "Hola, puede aplicar." }), { OPENAI_API_KEY: "k" }, fakeFetch);
  assert.equal(r.status, 200);
  assert.equal(r.headers.get("content-type"), "audio/mpeg");
  assert.equal(seen.body.input, "Hola, puede aplicar.");
  assert.match(seen.body.instructions, /mexicano/);
  assert.equal(seen.auth, "Bearer k");
  assert.ok(seen.signal instanceof AbortSignal, "the vendor call carries a timeout signal");
  const empty = await handleTts(post({ lang: "es", text: "" }), { OPENAI_API_KEY: "k" }, fakeFetch);
  assert.equal(empty.status, 400);
});

test("tts answers 504 on a vendor timeout and 502 on a vendor error", async () => {
  const timeout = await handleTts(post({ lang: "en", text: "Hello there." }), { OPENAI_API_KEY: "k" }, async () => { throw new DOMException("timed out", "TimeoutError"); });
  assert.equal(timeout.status, 504);
  assert.equal((await timeout.json()).error, "tts_failed");
  const failed = await handleTts(post({ lang: "en", text: "Hello there." }), { OPENAI_API_KEY: "k" }, async () => new Response("nope", { status: 429 }));
  assert.equal(failed.status, 502);
  assert.equal((await failed.json()).error, "tts_failed");
});
