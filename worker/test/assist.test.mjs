import { test } from "node:test";
import assert from "node:assert/strict";
import { handleHelp, handleExtract, EXTRACT_SCHEMA, helpSystem, programFacts, extractSystem, trimHistory } from "../src/assist.js";

const season = { season: "2026", opens: "2026-10-15T00:00:00", closes: "2026-11-15T23:59:59", mode: "pickup", announcement: { en: "", es: "" },
  help_phone: "+15305550100", help_email: "info@example.org", service_area: { cities: ["Truckee", "Soda Springs"], zips: [] } };
const req = (body) => new Request("https://api.test/x", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });

test("help passes program facts and answers", async () => {
  let seen;
  const ask = async (p) => { seen = p; return { stop_reason: "end_turn", content: [{ type: "text", text: "Sí, puede aplicar." }] }; };
  const r = await handleHelp(req({ lang: "es", question: "¿Puedo aplicar si vivo en Soda Springs?", history: [{ role: "user", content: "hola" }, { role: "assistant", content: "Hola." }] }), {}, season, ask);
  assert.equal(r.status, 200);
  assert.equal(r.data.answer, "Sí, puede aplicar.");
  assert.equal(seen.model, "claude-opus-5");
  assert.match(seen.system[0].text, /Soda Springs/);
  assert.equal(seen.messages.length, 3);
  assert.match(helpSystem(season, "en"), /never asks about immigration status/i);
});

test("help system says what the assistant is and is not", () => {
  const s = helpSystem(season, "en");
  assert.match(s, /You are an automated assistant, not a volunteer; if asked whether you are a person say you are a computer program\./);
  assert.match(s, /You cannot see any application, confirmation code, or decision; if asked about status say the family will hear by text in early December and give the phone number\./);
  assert.match(s, /Do not ask for a name, phone number, address, or other personal details\./);
  assert.match(s, /Do not promise that pickup day is safe from immigration enforcement or give legal advice; state the current plan and give the phone number\./);
  assert.doesNotMatch(s, /Current notice from the board/);
  assert.doesNotMatch(s, /word for word/);
});

test("the board's announcement goes into the prompt in the form's language", () => {
  const notice = { ...season, announcement: { en: "Pickup is cancelled. Cards will be mailed.", es: "Se cancela la entrega en persona. Las tarjetas se enviarán por correo." } };
  const es = helpSystem(notice, "es");
  assert.match(es, /Current notice from the board: Se cancela la entrega en persona\. Las tarjetas se enviarán por correo\./);
  assert.doesNotMatch(es, /Pickup is cancelled\./);
  assert.match(es, /repeat the current notice from the board word for word/);
  assert.match(es, /asked about pickup, dates, or coming in person/);
  const en = helpSystem(notice, "en");
  assert.match(en, /Current notice from the board: Pickup is cancelled\. Cards will be mailed\./);
  // Only one language filled in: use it rather than say nothing.
  const enOnly = helpSystem({ ...season, announcement: { en: "Only English here.", es: "" } }, "es");
  assert.match(enOnly, /Current notice from the board: Only English here\./);
  assert.doesNotMatch(helpSystem({ ...season, announcement: { en: "  ", es: "" } }, "en"), /Current notice/);
  assert.doesNotMatch(helpSystem({ ...season, announcement: undefined }, "en"), /Current notice/);
});

test("program facts follow the season mode", () => {
  const pickup = programFacts(season, "en");
  assert.match(pickup, /Bring the card on pickup day\./);
  assert.match(pickup, /in-person pickup/);
  const mail = programFacts({ ...season, mode: "mail" }, "en");
  assert.doesNotMatch(mail, /pickup day/);
  assert.match(mail, /card and gifts arrive by mail\. There is no pickup\./);
  assert.doesNotMatch(helpSystem({ ...season, mode: "mail" }, "en"), /pickup day/);
  const deliver = programFacts({ ...season, mode: "deliver" }, "en");
  assert.doesNotMatch(deliver, /pickup day/);
  assert.match(deliver, /a volunteer brings the card and gifts to the home\. There is no pickup\./);
  assert.doesNotMatch(helpSystem({ ...season, mode: "deliver" }, "es"), /pickup day/);
});

test("help history is capped at 6000 characters and starts on a user turn", async () => {
  let seen;
  const ask = async (p) => { seen = p; return { stop_reason: "end_turn", content: [{ type: "text", text: "ok" }] }; };
  const big = (role, n) => ({ role, content: role[0].repeat(n) });
  const history = [big("user", 2500), big("assistant", 2500), big("user", 2500), big("assistant", 2500)];
  const r = await handleHelp(req({ lang: "en", question: "hi", history }), {}, season, ask);
  assert.equal(r.status, 200);
  const turns = seen.messages.slice(0, -1);
  assert.ok(turns.reduce((n, m) => n + m.content.length, 0) <= 6000);
  assert.equal(turns.length, 2);
  assert.equal(turns[0].role, "user");
  assert.equal(seen.messages.at(-1).content, "hi");
  // Newest turns win; an orphaned assistant turn at the front is dropped.
  const t = trimHistory([big("assistant", 5000), big("user", 100), big("assistant", 500)]);
  assert.deepEqual(t.map((m) => [m.role, m.content.length]), [["user", 100], ["assistant", 500]]);
  // Each turn is cut to 4000 first, so one long turn still fits; two do not, and the orphan is dropped.
  assert.deepEqual(trimHistory([big("user", 7000)]).map((m) => m.content.length), [4000]);
  assert.deepEqual(trimHistory([big("user", 4000), big("assistant", 4000)]), []);
  assert.equal(trimHistory([{ role: "system", content: "x" }, { role: "user", content: 5 }, { role: "user", content: "fine" }]).length, 1);
  assert.equal(trimHistory("junk").length, 0);
  assert.equal(trimHistory(Array.from({ length: 20 }, () => big("user", 10))).length, 8);
});

test("extract returns schema-shaped fields", async () => {
  const fields = { first_name: "Rosa", last_name: "Lopez", phone: "5305550100", can_text: true, other_adult: "", street: "1 Elm St", unit: "", city: "Truckee", zip: "96161", mail_street: "", adults: 2, adult_coat_sizes: [], children: [{ first_name: "Diego", age: 3, sex: "boy", coat: true }], want_food: true, want_toys: true, want_coats: true };
  let seen;
  const ask = async (p) => { seen = p; assert.equal(p.output_config.format.schema, EXTRACT_SCHEMA); return { stop_reason: "end_turn", content: [{ type: "text", text: JSON.stringify({ fields, missing: [], summary: "ok" }) }] }; };
  const r = await handleExtract(req({ lang: "en", text: "I'm Rosa Lopez, 530 555 0100, 1 Elm St Truckee 96161, two adults, my son Diego is 3 and needs a coat" }), {}, season, ask);
  assert.equal(r.status, 200);
  assert.equal(r.data.fields.children[0].first_name, "Diego");
  assert.equal(seen.max_tokens, 1000);
  const short = await handleExtract(req({ lang: "en", text: "" }), {}, season, ask);
  assert.equal(short.status, 400);
});

test("extract: a child's age is null when not stated, and passes through untouched", async () => {
  assert.deepEqual(EXTRACT_SCHEMA.properties.fields.properties.children.items.properties.age, { anyOf: [{ type: "integer" }, { type: "null" }] });
  assert.match(extractSystem("en"), /null when the age was not stated, 0 only when they said baby, newborn, or under one/);
  assert.match(extractSystem("es"), /use "" or 0 or \[\] for anything not stated/);
  const fields = { first_name: "Rosa", last_name: "", phone: "", can_text: true, other_adult: "", street: "", unit: "", city: "", zip: "", mail_street: "", adults: 0, adult_coat_sizes: [],
    children: [{ first_name: "Diego", age: null, sex: "boy", coat: false }, { first_name: "Ana", age: 0, sex: "girl", coat: false }], want_food: false, want_toys: false, want_coats: false };
  const ask = async () => ({ stop_reason: "end_turn", content: [{ type: "text", text: JSON.stringify({ fields, missing: ["children's ages"], summary: "ok" }) }] });
  const r = await handleExtract(req({ lang: "en", text: "Diego and baby Ana" }), {}, season, ask);
  assert.equal(r.status, 200);
  assert.strictEqual(r.data.fields.children[0].age, null);
  assert.strictEqual(r.data.fields.children[1].age, 0);
  assert.deepEqual(r.data.missing, ["children's ages"]);
});

test("help and extract treat a null or non-object body as empty, not as a crash", async () => {
  const ask = async () => { throw new Error("must not be called"); };
  const rawReq = (raw) => new Request("https://api.test/x", { method: "POST", headers: { "content-type": "application/json" }, body: raw });
  for (const raw of ["null", "7", '"text"', "[]"]) {
    const h = await handleHelp(rawReq(raw), {}, season, ask);
    assert.equal(h.status, 400, raw);
    assert.equal(h.data.error, "empty");
    const x = await handleExtract(rawReq(raw), {}, season, ask);
    assert.equal(x.status, 400, raw);
    assert.equal(x.data.error, "too_short");
  }
});
