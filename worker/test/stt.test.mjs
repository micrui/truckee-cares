import { test } from "node:test";
import assert from "node:assert/strict";
import { handleTranscribe } from "../src/stt.js";

test("transcribe forwards the audio with the language and returns text", async () => {
  let seen;
  const fakeFetch = async (url, opts) => { seen = { url, model: opts.body.get("model"), language: opts.body.get("language"), hasFile: opts.body.get("file") instanceof Blob }; return new Response(JSON.stringify({ text: " Soy Rosa López. " }), { status: 200 }); };
  const form = new FormData();
  form.append("file", new Blob([new Uint8Array([1, 2, 3])], { type: "audio/webm" }), "a.webm");
  form.append("lang", "es");
  const r = await handleTranscribe(new Request("https://api.test/api/transcribe", { method: "POST", body: form }), { OPENAI_API_KEY: "k" }, fakeFetch);
  assert.equal(r.status, 200);
  assert.equal((await r.json()).text, "Soy Rosa López.");
  assert.equal(seen.language, "es");
  assert.equal(seen.hasFile, true);
  const empty = await handleTranscribe(new Request("https://api.test/api/transcribe", { method: "POST", body: new FormData() }), { OPENAI_API_KEY: "k" }, fakeFetch);
  assert.equal(empty.status, 400);
});
