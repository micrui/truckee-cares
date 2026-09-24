import { test } from "node:test";
import assert from "node:assert/strict";
import { handleTranscribe, MAX_BYTES } from "../src/stt.js";

const clip = (bytes = 3) => { const form = new FormData(); form.append("file", new Blob([new Uint8Array(bytes)], { type: "audio/webm" }), "a.webm"); form.append("lang", "es"); return form; };
const post = (form, headers = {}) => new Request("https://api.test/api/transcribe", { method: "POST", headers, body: form });

test("transcribe forwards the audio with the language and returns text", async () => {
  let seen;
  const fakeFetch = async (url, opts) => { seen = { url, model: opts.body.get("model"), language: opts.body.get("language"), hasFile: opts.body.get("file") instanceof Blob, signal: opts.signal }; return new Response(JSON.stringify({ text: " Soy Rosa López. " }), { status: 200 }); };
  const r = await handleTranscribe(post(clip()), { OPENAI_API_KEY: "k" }, fakeFetch);
  assert.equal(r.status, 200);
  assert.equal((await r.json()).text, "Soy Rosa López.");
  assert.equal(seen.language, "es");
  assert.equal(seen.hasFile, true);
  assert.ok(seen.signal instanceof AbortSignal, "the vendor call carries a timeout signal");
  const empty = await handleTranscribe(post(new FormData()), { OPENAI_API_KEY: "k" }, fakeFetch);
  assert.equal(empty.status, 400);
});

test("transcribe rejects clips over 2 MB before reading the body", async () => {
  assert.equal(MAX_BYTES, 2 * 1024 * 1024);
  let called = false;
  const fakeFetch = async () => { called = true; return new Response("{}", { status: 200 }); };
  const declared = await handleTranscribe(post(clip(), { "content-length": String(3 * 1024 * 1024) }), { OPENAI_API_KEY: "k" }, fakeFetch);
  assert.equal(declared.status, 413);
  assert.equal((await declared.json()).error, "too_large");
  const actual = await handleTranscribe(post(clip(MAX_BYTES + 1)), { OPENAI_API_KEY: "k" }, fakeFetch);
  assert.equal(actual.status, 413);
  assert.equal(called, false);
  const fine = await handleTranscribe(post(clip(1024), { "content-length": "2000" }), { OPENAI_API_KEY: "k" }, async () => new Response(JSON.stringify({ text: "ok" }), { status: 200 }));
  assert.equal(fine.status, 200);
});

test("transcribe answers 504 when the vendor call times out and 502 when it fails", async () => {
  const timeout = await handleTranscribe(post(clip()), { OPENAI_API_KEY: "k" }, async () => { throw new DOMException("The operation was aborted due to timeout", "TimeoutError"); });
  assert.equal(timeout.status, 504);
  assert.equal((await timeout.json()).error, "transcribe_failed");
  const failed = await handleTranscribe(post(clip()), { OPENAI_API_KEY: "k" }, async () => new Response("nope", { status: 500 }));
  assert.equal(failed.status, 502);
  assert.equal((await failed.json()).error, "transcribe_failed");
});
