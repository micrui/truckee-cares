#!/usr/bin/env node
// Pre-renders the read-aloud audio for every form screen in both languages with
// OpenAI text-to-speech. Output: site/static/audio/<lang>/<step>.mp3 plus
// site/static/audio/manifest.json (text hashes, so unchanged screens are not
// re-rendered and re-billed). Commit the MP3s; the site serves them as static files.
//
//   npm run audio            render what changed
//   npm run audio -- --force render everything
//
// Key: OPENAI_API_KEY, or ~/.config/truckee-cares/openai-key (bin/set-openai-key).
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { STRINGS } from "../site/static/js/apply-strings.js";
const season = JSON.parse(readFileSync(new URL("../config/season.json", import.meta.url), "utf8"));

// The opening date, spoken the way the screen shows it.
function opensDate(lang) {
  return new Date(season.opens + "Z").toLocaleDateString(lang === "es" ? "es-MX" : "en-US", { month: "long", day: "numeric", timeZone: "UTC" });
}

export const MODEL = "gpt-4o-mini-tts";
export const VOICES = { en: "nova", es: "nova" };
export const INSTRUCTIONS = {
  en: "Warm, calm, unhurried volunteer at a community nonprofit. Plain American English. Pause briefly between items.",
  es: "Voluntaria cálida y tranquila de una organización comunitaria. Español mexicano natural, claro y sin prisa, tratando de usted. Pausa breve entre cada punto.",
};

// What is read aloud on each screen. Keys refer to apply-strings.js. Keep this the
// spoken script, not the exact on-screen text: labels read better with a little glue.
export function narration(lang) {
  const s = STRINGS[lang];
  const opt = ` ${s.optional}`;
  return {
    welcome: [s.welcome_title, s.welcome_intro, s.welcome_time, s.welcome_rules_title, ...s.welcome_rules, s.phone_path_title, s.phone_path_text, s.freeform_title, s.freeform_text],
    you: [s.you_title, s.helper_q, s.helper_hint, s.you_why, s.first_name, s.last_name, s.phone, s.phone_hint, s.can_text, s.other_phone + opt, s.email + opt, s.other_adult + opt, s.other_adult_hint, s.contact_lang],
    home: [s.home_title, s.home_why, s.street, s.street_hint, s.unit + opt, s.city, s.zip, s.mail_same, s.mail_why],
    household: [s.household_title, s.adults, s.adult_coats, s.adult_coat_hint],
    children: [s.children_title, s.children_why, s.child_first, s.child_age, s.child_age_hint, s.child_sex, s.child_school + opt, s.child_coat, s.add_child, s.no_children, s.no_children_note],
    programs: [s.programs_title, s.want_food, s.want_toys, s.want_coats, s.referral + opt, s.referral_hint, s.notes + opt],
    review: [s.review_title, s.review_text, s.consent_area, s.consent_one, s.consent_true, s.remember, s.remember_hint, s.review_privacy],
    done: [s.done_title, s.done_code, s.done_text, s.done_limited],
    not_open: [s.not_open_title, s.not_open_text(opensDate(lang))],
    voice_intro: [s.voice_intro],
    voice_q_who: [s.voice_q_who], voice_q_home: [s.voice_q_home], voice_q_mail: [s.voice_q_mail], voice_q_adults: [s.voice_q_adults],
    voice_q_children: [s.voice_q_children], voice_q_other: [s.voice_q_other], voice_q_notes: [s.voice_q_notes],
    voice_m_first_name: [s.voice_missing_intro, s.voice_m_first_name], voice_m_last_name: [s.voice_missing_intro, s.voice_m_last_name],
    voice_m_phone: [s.voice_missing_intro, s.voice_m_phone], voice_m_street: [s.voice_missing_intro, s.voice_m_street],
    voice_m_zip: [s.voice_missing_intro, s.voice_m_zip], voice_m_city: [s.voice_missing_intro, s.voice_m_city],
    voice_m_adults: [s.voice_missing_intro, s.voice_m_adults], voice_m_children: [s.voice_missing_intro, s.voice_m_children],
    voice_consents: [s.voice_summary_intro],
    voice_consents_text: [s.voice_consents],
    closed: [s.closed_title, s.closed_text],
  };
}

function loadKey() {
  // The file written by bin/set-openai-key wins over a stale shell variable.
  const p = join(homedir(), ".config", "truckee-cares", "openai-key");
  const key = existsSync(p) ? readFileSync(p, "utf8").trim() : (process.env.OPENAI_API_KEY || "");
  if (!key) return null;
  if (!key.startsWith("sk-")) {
    console.error(`${existsSync(p) ? p : "OPENAI_API_KEY"} does not look like an OpenAI secret key (they start with sk-). The dashboard's "Tracking ID" (key_…) is not the secret. Run bin/set-openai-key again with the secret.`);
    process.exit(1);
  }
  return key;
}

export async function synthesize(key, lang, text) {
  const res = await fetch("https://api.openai.com/v1/audio/speech", {
    method: "POST",
    headers: { authorization: `Bearer ${key}`, "content-type": "application/json" },
    body: JSON.stringify({ model: MODEL, voice: VOICES[lang], input: text, instructions: INSTRUCTIONS[lang], response_format: "mp3" }),
  });
  if (!res.ok) throw new Error(`tts ${res.status}: ${await res.text()}`);
  return Buffer.from(await res.arrayBuffer());
}

async function main() {
  const force = process.argv.includes("--force");
  const root = new URL("../site/static/audio/", import.meta.url);
  const manifestPath = new URL("manifest.json", root);
  const manifest = existsSync(manifestPath) ? JSON.parse(readFileSync(manifestPath, "utf8")) : { model: MODEL, files: {} };
  const key = loadKey();
  let rendered = 0, chars = 0;
  for (const lang of ["en", "es"]) {
    mkdirSync(new URL(`${lang}/`, root), { recursive: true });
    for (const [step, lines] of Object.entries(narration(lang))) {
      const text = lines.filter(Boolean).join(".\n").replace(/\.\.\n/g, ".\n");
      const hash = createHash("sha256").update(`${MODEL}|${VOICES[lang]}|${INSTRUCTIONS[lang]}|${text}`).digest("hex").slice(0, 16);
      const name = `${lang}/${step}`;
      const out = new URL(`${name}.mp3`, root);
      if (!force && manifest.files[name]?.hash === hash && existsSync(out)) continue;
      if (!key) { console.error(`no OpenAI key; would render ${name} (${text.length} chars). Run bin/set-openai-key.`); continue; }
      process.stdout.write(`rendering ${name} (${text.length} chars)… `);
      writeFileSync(out, await synthesize(key, lang, text));
      manifest.files[name] = { hash, chars: text.length, rendered: new Date().toISOString() };
      rendered++; chars += text.length;
      console.log("ok");
    }
  }
  manifest.model = MODEL;
  writeFileSync(manifestPath, JSON.stringify(manifest, null, 2) + "\n");
  console.log(`rendered ${rendered} files, ${chars} characters`);
}

if (import.meta.url === `file://${process.argv[1]}`) main().catch((e) => { console.error(e); process.exit(1); });
