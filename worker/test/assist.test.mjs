import { test } from "node:test";
import assert from "node:assert/strict";
import { handleHelp, handleExtract, EXTRACT_SCHEMA, helpSystem } from "../src/assist.js";

const season = { season: "2026", opens: "2026-10-15T00:00:00", closes: "2026-11-15T23:59:59", mode: "pickup",
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

test("extract returns schema-shaped fields", async () => {
  const fields = { first_name: "Rosa", last_name: "Lopez", phone: "5305550100", other_adult: "", street: "1 Elm St", unit: "", city: "Truckee", zip: "96161", mail_street: "", adults: 2, adult_coat_sizes: [], children: [{ first_name: "Diego", age: 3, sex: "boy", coat: true }], want_food: true, want_toys: true, want_coats: true };
  const ask = async (p) => { assert.equal(p.output_config.format.schema, EXTRACT_SCHEMA); return { stop_reason: "end_turn", content: [{ type: "text", text: JSON.stringify({ fields, missing: [], summary: "ok" }) }] }; };
  const r = await handleExtract(req({ lang: "en", text: "I'm Rosa Lopez, 530 555 0100, 1 Elm St Truckee 96161, two adults, my son Diego is 3 and needs a coat" }), {}, season, ask);
  assert.equal(r.status, 200);
  assert.equal(r.data.fields.children[0].first_name, "Diego");
  const short = await handleExtract(req({ lang: "en", text: "hi" }), {}, season, ask);
  assert.equal(short.status, 400);
});
