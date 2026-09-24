import { test } from "node:test";
import assert from "node:assert/strict";
import { handleTts } from "../src/tts.js";

test("tts proxies to OpenAI with the Spanish voice and streams audio back", async () => {
  let seen;
  const fakeFetch = async (url, opts) => { seen = { url, body: JSON.parse(opts.body), auth: opts.headers.authorization }; return new Response(new Uint8Array([1, 2, 3]), { status: 200 }); };
  const req = new Request("https://api.test/api/tts", { method: "POST", body: JSON.stringify({ lang: "es", text: "Hola, puede aplicar." }) });
  const r = await handleTts(req, { OPENAI_API_KEY: "k" }, fakeFetch);
  assert.equal(r.status, 200);
  assert.equal(r.headers.get("content-type"), "audio/mpeg");
  assert.equal(seen.body.input, "Hola, puede aplicar.");
  assert.match(seen.body.instructions, /mexicano/);
  assert.equal(seen.auth, "Bearer k");
  const empty = await handleTts(new Request("https://api.test/api/tts", { method: "POST", body: JSON.stringify({ lang: "es", text: "" }) }), { OPENAI_API_KEY: "k" }, fakeFetch);
  assert.equal(empty.status, 400);
});
