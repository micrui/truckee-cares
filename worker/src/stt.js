// Speech to text for the "tell us in your own words" box.
//   POST /api/transcribe  multipart: file=<audio>, lang=en|es  -> { text }
// Audio goes to OpenAI for transcription and is not stored anywhere by us.
export const MODEL = "gpt-4o-mini-transcribe";
const MAX_BYTES = 12 * 1024 * 1024;

export async function handleTranscribe(req, env, fetchFn = fetch) {
  let form;
  try { form = await req.formData(); } catch { return json({ error: "bad_form" }, 400); }
  const file = form.get("file");
  const lang = form.get("lang") === "es" ? "es" : "en";
  if (!file || typeof file === "string") return json({ error: "no_file" }, 400);
  if (file.size > MAX_BYTES) return json({ error: "too_large" }, 413);
  const out = new FormData();
  out.append("file", file, file.name || "audio.webm");
  out.append("model", MODEL);
  out.append("language", lang);
  out.append("response_format", "json");
  const r = await fetchFn("https://api.openai.com/v1/audio/transcriptions", { method: "POST", headers: { authorization: `Bearer ${env.OPENAI_API_KEY}` }, body: out });
  if (!r.ok) return json({ error: "transcribe_failed" }, 502);
  const data = await r.json();
  return json({ text: String(data.text || "").trim() }, 200);
}

function json(data, status) {
  return new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json", "cache-control": "no-store" } });
}
