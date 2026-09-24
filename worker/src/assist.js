// Live help inside the application form, backed by the Claude API.
//
//   POST /api/help     <- { lang, question, history:[{role,content}] }  -> { answer }
//   POST /api/extract  <- { lang, text }                                 -> { fields, missing, summary }
//
// Nothing from these calls is stored by the Worker. Only the person's own
// words go to the model, plus program facts from config/season.json.
import Anthropic from "@anthropic-ai/sdk";

export const MODEL = "claude-opus-5";
const MAX_TEXT = 4000;

function programFacts(season) {
  return `Program facts for the ${season.season} season:
- Truckee Community Cares (TCC) is an all-volunteer nonprofit in Truckee, California. It gives families a grocery gift card, one new toy per child (ages 0-18), and warm coats for family members who need one, in mid-December.
- Who can apply: families living in ${season.service_area.cities.join(" or ")}. Proof of address may be asked for on pickup day. One application per family per year.
- Applications open ${season.opens.slice(0, 10)} and close ${season.closes.slice(0, 10)}. Late applications are not accepted.
- After applying: if accepted, the family gets a text and a card by mail in early December. Bring the card on pickup day. Funds are limited, so not every family can be helped. Current plan: ${season.mode === "pickup" ? "in-person pickup in mid-December" : season.mode === "mail" ? "cards mailed to families" : "delivery to families"}.
- Seniors: low-income and home-bound seniors get grocery cards through Sierra Senior Services; they do not fill out this form.
- TCC never asks about immigration status, income documents, Social Security numbers, or dates of birth. Answers are encrypted on the phone before sending. Applications are deleted after the season.
- Help from a person: text or call ${season.help_phone}, or email ${season.help_email}. WhatsApp works on the same number.`;
}

export function helpSystem(season, lang) {
  return `You are the help assistant inside the Truckee Community Cares holiday-assistance application form. You talk to families, many of them Spanish-speaking immigrants, some helped by a volunteer. Be warm, plain, and brief: two or three short sentences, no lists unless asked. Never ask about immigration status. Never promise that a family will be accepted. If you do not know something, say so and point to the phone number. If the person seems to need a human, or asks for one, give the phone number right away. Answer in ${lang === "es" ? "Mexican Spanish, using usted" : "plain English"} unless they write in the other language.

${programFacts(season)}

The form has these steps: 1 about you (name, phone, whether we may text, another adult in the home), 2 where you live (street address, mailing address), 3 adults in the home and adult coat sizes, 4 children (first name, age, boy or girl, school, coat), 5 what would help (grocery card, toys, coats), 6 review and send. A confirmation code is shown at the end; they should screenshot it.`;
}

export const EXTRACT_SCHEMA = {
  type: "object",
  properties: {
    fields: {
      type: "object",
      properties: {
        first_name: { type: "string" }, last_name: { type: "string" }, phone: { type: "string" }, can_text: { type: "boolean" },
        other_adult: { type: "string" }, street: { type: "string" }, unit: { type: "string" }, city: { type: "string" }, zip: { type: "string" },
        mail_street: { type: "string" }, adults: { type: "integer" },
        adult_coat_sizes: { type: "array", items: { type: "string" } },
        children: { type: "array", items: { type: "object", properties: { first_name: { type: "string" }, age: { type: "integer" }, sex: { type: "string", enum: ["boy", "girl", ""] }, coat: { type: "boolean" } }, required: ["first_name", "age", "sex", "coat"], additionalProperties: false } },
        want_food: { type: "boolean" }, want_toys: { type: "boolean" }, want_coats: { type: "boolean" },
      },
      required: ["first_name", "last_name", "phone", "can_text", "other_adult", "street", "unit", "city", "zip", "mail_street", "adults", "adult_coat_sizes", "children", "want_food", "want_toys", "want_coats"],
      additionalProperties: false,
    },
    missing: { type: "array", items: { type: "string" } },
    summary: { type: "string" },
  },
  required: ["fields", "missing", "summary"],
  additionalProperties: false,
};

export function extractSystem(lang) {
  return `You turn what a person says about their family into fields for a holiday-assistance form. Copy only what they said; use "" or 0 or [] for anything not stated, never guess. Phone as 10 digits (people often say the digits in groups, or in Spanish). can_text is true if they agreed to texts, or said nothing about it; false only if they declined. City is Truckee, Soda Springs, or what they said. Children: first name, age in years (0 for a baby), boy or girl if stated, coat true only if they said the child needs a coat. adults is the number of adults in the home if stated, else 0. want_food, want_toys, want_coats are true only if they asked for that. When a question is given, fill only what that answer says; leave everything else empty. A plain "no" or "yes" answer means empty fields. "missing" lists, in ${lang === "es" ? "Spanish" : "English"}, the things the form still needs that they did not say (for example "children's ages"). "summary" is one warm sentence in ${lang === "es" ? "Mexican Spanish (usted)" : "plain English"} repeating back what you understood so they can check it.`;
}

// `ask` is injectable so tests never touch the network.
export function makeAsk(env) {
  const client = new Anthropic({ apiKey: env.ANTHROPIC_API_KEY });
  return (params) => client.messages.create(params);
}

export async function handleHelp(req, env, season, ask = makeAsk(env)) {
  let body; try { body = await req.json(); } catch { return { status: 400, data: { error: "bad_json" } }; }
  const lang = body.lang === "es" ? "es" : "en";
  const history = Array.isArray(body.history) ? body.history.slice(-8) : [];
  const question = String(body.question || "").slice(0, MAX_TEXT).trim();
  if (!question) return { status: 400, data: { error: "empty" } };
  const messages = history
    .filter((m) => (m.role === "user" || m.role === "assistant") && typeof m.content === "string")
    .map((m) => ({ role: m.role, content: m.content.slice(0, MAX_TEXT) }));
  messages.push({ role: "user", content: question });
  const resp = await ask({
    model: MODEL, max_tokens: 600,
    system: [{ type: "text", text: helpSystem(season, lang), cache_control: { type: "ephemeral" } }],
    output_config: { effort: "low" },
    messages,
  });
  if (resp.stop_reason === "refusal") return { status: 200, data: { answer: lang === "es" ? `No puedo ayudar con eso aquí. Mande un texto al ${season.help_phone}.` : `I can't help with that here. Text ${season.help_phone}.` } };
  const answer = resp.content.filter((b) => b.type === "text").map((b) => b.text).join("").trim();
  return { status: 200, data: { answer } };
}

export async function handleExtract(req, env, season, ask = makeAsk(env)) {
  let body; try { body = await req.json(); } catch { return { status: 400, data: { error: "bad_json" } }; }
  const lang = body.lang === "es" ? "es" : "en";
  const text = String(body.text || "").slice(0, MAX_TEXT).trim();
  if (text.length < 2) return { status: 400, data: { error: "too_short" } };
  // Voice mode sends one answer at a time with the question that was asked.
  const question = typeof body.question === "string" ? body.question.slice(0, 400) : "";
  const content = question ? `The person was asked: "${question}"\nThey answered: ${text}` : text;
  const resp = await ask({
    model: MODEL, max_tokens: 4000,
    system: [{ type: "text", text: extractSystem(lang), cache_control: { type: "ephemeral" } }],
    output_config: { effort: "low", format: { type: "json_schema", schema: EXTRACT_SCHEMA } },
    messages: [{ role: "user", content }],
  });
  if (resp.stop_reason === "refusal") return { status: 200, data: { fields: null, missing: [], summary: "" } };
  const out = JSON.parse(resp.content.find((b) => b.type === "text").text);
  return { status: 200, data: out };
}
