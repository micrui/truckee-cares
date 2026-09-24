// Dynamic text-to-speech for text that is not known at build time: help-chat
// answers, the free-form summary, the confirmation screen.
//   POST /api/tts <- { lang, text } -> audio/mpeg
// Uses the same OpenAI voice as the pre-rendered screens. Nothing is stored.
export const MODEL = "gpt-4o-mini-tts";
export const VOICES = { en: "nova", es: "nova" };
export const INSTRUCTIONS = {
  en: "Warm, calm, unhurried volunteer at a community nonprofit. Plain American English.",
  es: "Voluntaria cálida y tranquila de una organización comunitaria. Español mexicano natural, claro y sin prisa, tratando de usted.",
};
const MAX_CHARS = 800;
const TIMEOUT_MS = 30000;

export async function handleTts(req, env, fetchFn = fetch) {
  let body; try { body = await req.json(); } catch { return new Response(JSON.stringify({ error: "bad_json" }), { status: 400 }); }
  const lang = body.lang === "es" ? "es" : "en";
  const text = String(body.text || "").trim().slice(0, MAX_CHARS);
  if (text.length < 2) return new Response(JSON.stringify({ error: "empty" }), { status: 400 });
  let r;
  try {
    r = await fetchFn("https://api.openai.com/v1/audio/speech", {
      method: "POST",
      headers: { authorization: `Bearer ${env.OPENAI_API_KEY}`, "content-type": "application/json" },
      body: JSON.stringify({ model: MODEL, voice: VOICES[lang], input: text, instructions: INSTRUCTIONS[lang], response_format: "mp3" }),
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch {
    return new Response(JSON.stringify({ error: "tts_failed" }), { status: 504 });
  }
  if (!r.ok) return new Response(JSON.stringify({ error: "tts_failed" }), { status: 502 });
  return new Response(r.body, { status: 200, headers: { "content-type": "audio/mpeg", "cache-control": "no-store" } });
}
