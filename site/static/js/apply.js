// Truckee Community Cares application form.
// One screen per step, no framework. State lives in memory and (as a draft) in
// sessionStorage. On submit the answers are encrypted in the browser with age
// to the season recipients in config.json and POSTed as ciphertext. The server
// never sees plaintext. Optional "remember me" keeps a copy in localStorage on
// nothing is kept on the device after a send.
import { STRINGS, ADULT_SIZES, CHILD_SIZES } from "./apply-strings.js";
import { Encrypter, Decrypter, armor } from "./age.js";

// One decision per screen. The list depends on the answers so far (helper details,
// mailing address, one pair of screens per child), so it is computed, not fixed.
function screens() {
  const list = ["welcome", "helper"];
  if (state.helper === "yes") list.push("helper_info");
  list.push("name", "phone", "street", "cityzip", "mail");
  if (state.mail_same === "no") list.push("mail_addr");
  list.push("adults", "adult_coats", "has_children");
  if (state.has_children === "yes") state.children.forEach((_, i) => list.push(`child:${i}:a`, `child:${i}:b`, `child:${i}:more`));
  list.push("referral", "review");
  return list;
}
let cur = "welcome";
let editReturn = null;   // set when Edit is tapped on the review screen
let supersedes = "";     // code of the application this send replaces (an edit)
let check = { open: false, code: "", busy: false, result: null, error: "" };   // "Check my application"
let sentStatus = null;   // live status of the known application, for the welcome card
let copyError = "";      // shown on the status card when the saved copy cannot be opened
async function fetchStatus(code, withCopy = false) {
  const r = await fetch(`${config.api_base}/api/status/${encodeURIComponent(code)}${withCopy ? "?copy=1" : ""}`, { cache: "no-store", headers: apiHeaders() });
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(r.status);
  return r.json();
}
function statusCard(st) {
  const key = ["fetched", "needs_info", "accepted", "declined", "duplicate", "out_of_area", "superseded"].includes(st.status) ? st.status : "fetched";
  const when = st.created_at ? new Date(st.created_at).toLocaleDateString(lang === "es" ? "es-MX" : "en-US", { month: "long", day: "numeric" }) : "";
  const head = key === "superseded" ? t("st_superseded", st.superseded_by || "") : t("st_" + key);
  const mode = st.mode && st.mode !== "pickup" ? st.mode : "";
  const textKey = key === "accepted" && mode ? `st_accepted_text_${mode}` : `st_${key}_text`;
  const canChange = ["fetched", "needs_info"].includes(key);
  return `<div class="card status-card"><p class="muted">${esc(st.id)} · ${esc(t("check_sent_on", when))}</p>
    <p class="q-title" style="margin:6px 0">${esc(head)}</p>
    ${st.note ? `<p class="why">${esc(st.note)}</p>` : ""}
    <p>${esc(t(textKey))}</p>
    ${key === "needs_info" ? `<p class="help-links"><a class="btn btn-primary" href="sms:${config.help_phone}">💬 ${t("help_text_msg")}</a><a class="btn btn-primary" href="tel:${config.help_phone}">📞 ${t("help_call")}</a></p>` : ""}
    ${canChange ? (codeKey(st.id) && st.has_copy
      ? `<button type="button" class="btn btn-primary" data-action="edit-copy" data-code="${esc(st.id)}" style="margin-top:12px">✏️ ${t("edit_resend")}</button>`
      : `<p class="muted" style="margin-top:12px">${t("st_change_other")}</p><button type="button" class="btn btn-ghost" data-action="redo" data-code="${esc(st.id)}">✏️ ${t("st_change_btn")}</button>`) : ""}
    ${copyError ? `<p class="error">${esc(copyError)}</p>` : ""}
  </div>`;
}
let lastSupersedes = ""; // shown on the done screen after an edit
function loadSent() {
  try {
    const d = JSON.parse(sessionStorage.getItem(SENT_KEY));
    if (d && d.id && d.state && Date.now() - Date.parse(d.sent_at || 0) < SENT_TTL_MS) return d;
    sessionStorage.removeItem(SENT_KEY);
  } catch (e) {}
  return null;
}
function clearSent() { try { sessionStorage.removeItem(SENT_KEY); sessionStorage.removeItem(DRAFT_KEY); } catch (e) {} }
const stepIndex = () => Math.max(0, screens().indexOf(cur));
let demoMode = false;   // ?voice: shows the voice-first and free-form demos on the welcome screen
const DRAFT_KEY = "tcc-draft";
const REMEMBER_KEY = "tcc-remembered";
const SENT_KEY = "tcc-sent";          // this session's sent application, so it can be edited
const SENT_TTL_MS = 2 * 3600 * 1000;
const CODES_KEY = "tcc-codes";        // confirmation codes sent from this phone this season (codes only, no answers)
function loadCodes() {
  // Codes from this season, plus test codes while the page is in preview mode.
  try { const d = JSON.parse(localStorage.getItem(CODES_KEY)); if (Array.isArray(d)) return d.filter((c) => c && c.id && (c.season === (config && config.season) || (previewMode && c.season === "preview"))); } catch (e) {}
  return [];
}
function rememberCode(id, seasonId, sentAt, key) {
  try {
    const all = (() => { try { const d = JSON.parse(localStorage.getItem(CODES_KEY)); return Array.isArray(d) ? d : []; } catch (e) { return []; } })();
    const prev = all.find((c) => c && c.id === id) || {};
    const entry = { id, season: seasonId || (previewMode ? "preview" : config.season), sent_at: sentAt || new Date().toISOString(), k: key || prev.k || "" };
    const list = [entry, ...all.filter((c) => c && c.id !== id)].slice(0, 5);
    localStorage.setItem(CODES_KEY, JSON.stringify(list));
  } catch (e) {}
}
const codeKey = (id) => (loadCodes().find((c) => c.id === id) || {}).k || "";
function randomKey() { const b = crypto.getRandomValues(new Uint8Array(16)); return btoa(String.fromCharCode(...b)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""); }
// The applicant's own copy of their answers: encrypted to a random key that never leaves
// this phone, stored on the server next to the application, opened only here.
async function makeSelfCopy(key) {
  const { consent_all, ...keep } = state;
  const enc = new Encrypter(); enc.setPassphrase(key); enc.setScryptWorkFactor(12);
  return armor.encode(await enc.encrypt(JSON.stringify(keep)));
}
async function openSelfCopy(code) {
  const key = codeKey(code); if (!key) throw new Error("no key");
  const st = await fetchStatus(code, true);
  if (!st || !st.self_copy) throw new Error("no copy");
  const dec = new Decrypter(); dec.addPassphrase(key);
  return JSON.parse(await dec.decrypt(armor.decode(st.self_copy), "text"));
}
function forgetCode(id) { try { localStorage.setItem(CODES_KEY, JSON.stringify(loadCodes().filter((c) => c.id !== id))); } catch (e) {} }
const LANG_KEY = "tcc-lang";

const root = document.getElementById("app");
const base = root.dataset.base || "";
let lang = root.dataset.lang || "en";
let config = null;
let strings = STRINGS[lang];
let state = blankState();
let errors = {};
let gate = { open: true, reason: null };
let prefilled = false;
let previewMode = false;   // ?preview before opening day: submissions go to the "preview" season
let previewToken = "";     // ?preview=<token> keeps preview working during the season
let scrollTop = true;      // render() scrolls to the top only after a navigation, not on a reveal
let sendReason = "";       // "" | "stale" | "busy" after a failed send
let voiceGen = 0;          // dropped late TTS responses after the screen changed

function blankState() {
  return {
    helper: "", helper_name: "", helper_phone: "", helper_org: "",
    first_name: "", last_name: "", phone: "", can_text: "", other_phone: "", email: "",
    other_adult: "", contact_lang: lang,
    street: "", unit: "", city: "", city_other: "", zip: "",
    mail_same: "", mail_street: "", mail_city: "", mail_zip: "",
    adults: "", adult_coats: "", adult_coat_sizes: [],
    children: [], no_children: false,
    want_food: true, want_toys: true, want_coats: true, referral: "", notes: "",
    has_children: "", referral_choice: "", consent_all: false, remember: false,
  };
}
function blankChild() { return { first_name: "", age: "", sex: "", school: "", school_other: "", coat: "", coat_size: "", more: "" }; }

// ---------- helpers ----------
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const digits = (s) => String(s || "").replace(/\D/g, "");
function t(key, ...args) {
  // Screens that talk about pickup have _mail and _deliver variants for the ICE fallback.
  const mode = config && config.mode && config.mode !== "pickup" ? config.mode : "";
  const v = (mode && strings[`${key}_${mode}`] !== undefined) ? strings[`${key}_${mode}`] : strings[key];
  return typeof v === "function" ? v(...args) : v ?? key;
}
const apiHeaders = (extra = {}) => ({ ...(previewToken ? { "x-preview": previewToken } : {}), ...extra });
function saveDraft() { try { sessionStorage.setItem(DRAFT_KEY, JSON.stringify({ cur, state, supersedes, editReturn })); } catch (e) {} }
function loadDraft() {
  try {
    const d = JSON.parse(sessionStorage.getItem(DRAFT_KEY));
    if (d && d.state) { state = { ...blankState(), ...d.state }; cur = screens().includes(d.cur) ? d.cur : "welcome"; supersedes = d.supersedes || ""; editReturn = d.editReturn || null; }
  } catch (e) {}
}
// Nothing about an application is kept on the device. Earlier builds could save answers
// for next year; that is gone, and any old copy is removed at boot.
function loadRemembered() { return null; }
function fmtDate(iso) { return new Date(iso).toLocaleDateString(lang === "es" ? "es-MX" : "en-US", { month: "long", day: "numeric", timeZone: config.timezone }); }
function localToUtc(iso, tz) {
  // Interpret a naive ISO time as wall-clock time in tz. Two passes handle DST edges.
  const target = Date.parse(iso + "Z");
  let guess = target;
  for (let i = 0; i < 2; i++) {
    const parts = new Intl.DateTimeFormat("en-US", { timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }).formatToParts(new Date(guess));
    const p = Object.fromEntries(parts.map((x) => [x.type, x.value]));
    const wall = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second);
    guess -= wall - target;
  }
  return guess;
}
function computeGate() {
  const now = Date.now();
  const opens = localToUtc(config.opens, config.timezone);
  const closes = localToUtc(config.closes, config.timezone);
  if (now < opens) return { open: false, reason: "not_open" };
  if (now > closes) return { open: false, reason: "closed" };
  return { open: true, reason: null };
}
function inArea() {
  const zips = config.service_area.zips;
  const cities = config.service_area.cities.map((c) => c.toLowerCase());
  return zips.includes(digits(state.zip)) || cities.includes((state.city || "").toLowerCase());
}
function setLang(l) {
  lang = l; strings = STRINGS[l];
  try { localStorage.setItem(LANG_KEY, l); } catch (e) {}
  document.documentElement.lang = l;
  const sw = document.querySelector("[data-lang-switch]"); if (sw) sw.hidden = true;
  render();
}

// ---------- validation ----------
function validate(id) {
  const e = {};
  const req = (k) => { if (!String(state[k] ?? "").trim()) e[k] = t("required"); };
  const pick = (k) => { if (!state[k]) e[k] = t("pick_one"); };
  if (id === "helper") pick("helper");
  if (id === "helper_info") { req("helper_name"); if (digits(state.helper_phone).length !== 10) e.helper_phone = t("bad_phone"); }
  if (id === "name") { req("first_name"); req("last_name"); }
  if (id === "phone") { if (digits(state.phone).length !== 10) e.phone = t("bad_phone"); pick("can_text"); }
  if (id === "street") req("street");
  if (id === "cityzip") { pick("city"); if (state.city === "other") req("city_other"); if (digits(state.zip).length !== 5) e.zip = t("bad_zip"); }
  if (id === "mail") pick("mail_same");
  if (id === "mail_addr") { req("mail_street"); if (state.mail_zip && digits(state.mail_zip).length !== 5) e.mail_zip = t("bad_zip"); }
  if (id === "adults") pick("adults");
  if (id === "adult_coats") { pick("adult_coats"); if (state.adult_coats === "yes" && !state.adult_coat_sizes.length) e.adult_coat_sizes = t("pick_one"); }
  if (id === "has_children") pick("has_children");
  const m = id.match(/^child:(\d+):(a|b|more)$/);
  if (m) {
    const c = state.children[+m[1]];
    if (!c) e.children = t("required");
    else if (m[2] === "a") {
      if (!c.first_name.trim()) e[`child_${m[1]}_first_name`] = t("required");
      const a = Number(c.age); if (c.age === "" || !Number.isInteger(a) || a < 0 || a > 18) e[`child_${m[1]}_age`] = t("bad_age");
    } else if (m[2] === "b") {
      if (!c.sex) e[`child_${m[1]}_sex`] = t("pick_one");
      if (!c.coat) e[`child_${m[1]}_coat`] = t("pick_one");
    } else if (!c.more) e[`child_${m[1]}_more`] = t("pick_one");
  }
  if (id === "review" && !state.consent_all) e.consent_all = t("required");
  return e;
}

// ---------- rendering ----------
function field(key, label, opts = {}) {
  const type = opts.type || "text"; const id = `f_${key}`;
  const value = opts.value ?? state[key] ?? "";
  const err = errors[opts.errKey || key];
  const attrs = `${opts.inputmode ? `inputmode="${opts.inputmode}"` : ""} ${opts.autocomplete ? `autocomplete="${opts.autocomplete}"` : ""} ${opts.maxlength ? `maxlength="${opts.maxlength}"` : ""}`;
  return `<div class="field ${err ? "invalid" : ""}">
    <label for="${id}">${esc(label)} ${opts.optional ? `<span class="muted">${t("optional")}</span>` : ""}</label>
    ${opts.hint ? `<div class="hint">${esc(opts.hint)}</div>` : ""}
    ${type === "textarea" ? `<textarea id="${id}" name="${key}" rows="3" maxlength="500">${esc(value)}</textarea>`
      : `<input id="${id}" name="${key}" type="${type}" value="${esc(value)}" ${attrs} ${err ? 'aria-invalid="true"' : ""}>`}
    ${err ? `<div class="msg" role="alert">${esc(err)}</div>` : ""}</div>`;
}
function choice(key, legend, options, opts = {}) {
  const err = errors[key]; const current = opts.value ?? state[key];
  const name = opts.name || key;
  return `<fieldset class="field ${err ? "invalid" : ""}"><legend>${esc(legend)} ${opts.optional ? `<span class="muted">${t("optional")}</span>` : ""}</legend>
    ${opts.hint ? `<div class="hint">${esc(opts.hint)}</div>` : ""}
    <div class="choices ${opts.stack ? "stack" : ""} ${opts.big ? "big" : ""}">${options.map(([v, l]) =>
      `<label><input type="radio" name="${name}" value="${esc(v)}" ${String(current) === String(v) ? "checked" : ""}> ${esc(l)}</label>`).join("")}</div>
    ${err ? `<div class="msg" role="alert">${esc(err)}</div>` : ""}</fieldset>`;
}
function select(key, label, options, opts = {}) {
  const id = `f_${key}`; const err = errors[opts.errKey || key]; const current = opts.value ?? state[key];
  return `<div class="field ${err ? "invalid" : ""}"><label for="${id}">${esc(label)} ${opts.optional ? `<span class="muted">${t("optional")}</span>` : ""}</label>
    <select id="${id}" name="${key}"><option value="">${t("choose")}</option>${options.map(([v, l]) => `<option value="${esc(v)}" ${String(current) === String(v) ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>
    ${err ? `<div class="msg" role="alert">${esc(err)}</div>` : ""}</div>`;
}
const yesno = () => [["yes", t("yes")], ["no", t("no")]];

function renderStep() {
  const id = cur;
  const list = screens();
  const last = id === "review";
  const nav = `<div class="nav-row">
    ${id !== "welcome" ? `<button class="btn btn-ghost" data-action="back">${t("back")}</button>` : ""}
    <button class="btn btn-primary" data-action="${last ? "submit" : "next"}">${last ? (supersedes ? t("replace_send") : t("review_send")) : editReturn ? t("done_editing") : t("next")}</button></div>`;
  const q = (text, hint) => `<h1 class="q-title">${esc(text)}</h1>${hint ? `<p class="why">${esc(hint)}</p>` : ""}`;

  if (id === "welcome") {
    const sent = null;
    const latest = loadCodes()[0] || null;
    const known = latest ? latest.id : "";
    const status = sentStatus && sentStatus.id === known ? sentStatus : null;
    const checkBox = check.open ? `<div class="card"><h2 class="q-title" style="font-size:1.25rem">${t("check_title")}</h2><p class="hint">${t("check_hint")}</p>
        <form data-action="check-form" class="assist-form"><input id="check-code" type="text" value="${esc(check.code)}" placeholder="TCC-26-ABCDE" autocapitalize="characters" autocomplete="off" spellcheck="false" maxlength="14" ${check.busy ? "disabled" : ""}><button class="btn btn-primary" ${check.busy ? "disabled" : ""}>${t("check_go")}</button></form>
        ${check.error ? `<p class="error">${esc(check.error)}</p>` : ""}</div>` : "";
    // Someone with an application on this phone: one card for it, then one clear next step.
    if (known) {
      return `<div class="step welcome"><h1>${t("welcome_title")}</h1>
        <div class="card"><p><strong>${t("your_application")}: ${esc(known)}</strong></p>
          ${status ? statusCard(status) : `<p class="muted">${t("checking")}</p>`}
          <p class="small-links"><button type="button" class="btn btn-ghost" data-action="forget-code" data-code="${esc(known)}">${t("forget_code")}</button></p></div>
        ${sent ? `<button class="btn btn-primary btn-big" data-action="edit-sent">✏️ ${t("sent_edit")}</button>` : ""}
        <button class="btn btn-secondary btn-big btn-hero" data-action="new-family" style="margin-top:10px">👨‍👩‍👧 ${t("apply_another")}</button>
        ${checkBox || `<p class="small-links"><button type="button" class="btn btn-ghost" data-action="check-open">🔎 ${t("check_other")}</button></p>`}
        <button type="button" class="btn btn-ghost btn-big" data-action="help-open" style="margin-top:12px">🆘 ${t("help_sheet_title")}</button>
      </div>`;
    }
    return `<div class="step welcome"><h1>${t("welcome_title")}</h1><p class="lead-short">${t("welcome_short")}</p>
      <button class="btn btn-secondary btn-big btn-hero" data-action="next">${t("start")}</button>
      ${checkBox || `<button type="button" class="btn btn-ghost btn-big" data-action="check-open" style="margin-top:12px">🔎 ${t("check_btn")}</button>`}
      <button type="button" class="btn btn-ghost btn-big" data-action="help-open" style="margin-top:12px">🆘 ${t("help_sheet_title")}</button>
      ${demoMode && voiceEnabled() ? `<div class="card" style="text-align:center"><button type="button" class="btn btn-secondary btn-big" data-action="voice-start" style="min-height:72px;font-size:1.25rem">🎤 ${t("voice_enter")}</button><p class="muted" style="margin:8px 0 0">${t("voice_enter_hint")}</p></div>` : ""}
      ${demoMode && assistEnabled() ? `<div class="card"><h2>🎤 ${t("freeform_title")}</h2><p>${t("freeform_text")}</p>
        ${sttEnabled() ? `<button type="button" class="btn btn-big ${rec.state === "recording" ? "btn-secondary recording" : "btn-primary"}" data-action="record" ${rec.state !== "idle" || freeform.busy ? "disabled" : ""}>
          ${rec.state === "recording" ? "⏺ " + t("speak_stop") : rec.state === "transcribing" ? t("speak_transcribing") : "🎤 " + t("speak_start")}</button>
          ${rec.error ? `<p class="error">${esc(rec.error)}</p>` : ""}` : ""}
        <div class="field" style="margin-top:12px"><textarea id="freeform" rows="5" placeholder="${esc(t("freeform_placeholder"))}" ${freeform.busy ? "disabled" : ""}>${esc(freeform.text)}</textarea></div>
        ${freeform.error ? `<p class="error">${t("freeform_error")}</p>` : ""}
        ${freeform.short ? `<p class="why">${t("freeform_short")}</p>` : ""}
        ${freeform.missing ? `<p class="why">${t("freeform_done")}${freeform.missing.length ? ` <strong>${t("freeform_missing")}</strong> ${esc(freeform.missing.join(", "))}` : ""}</p>` : ""}
        <button type="button" class="btn btn-secondary btn-big" data-action="freeform" ${freeform.busy ? "disabled" : ""}>${freeform.busy ? t("freeform_working") : t("freeform_go")}</button></div>` : ""}
    </div>`;
  }
  if (id === "helper") return `<div class="step">${q(t("helper_q"))}${choice("helper", "", [["no", t("helper_self")], ["yes", t("helper_other")]], { stack: true, big: true })}<p class="why">${esc(t("helper_hint"))}</p>${nav}</div>`;
  if (id === "helper_info") return `<div class="step">${q(t("helper_name"))}${field("helper_name", t("helper_name"))}${field("helper_phone", t("helper_phone"), { type: "tel", inputmode: "tel" })}${field("helper_org", t("helper_org"), { optional: true, hint: t("helper_org_hint") })}${nav}</div>`;
  if (id === "name") return `<div class="step">${q(t("q_name"))}${field("first_name", t("first_name"), { autocomplete: "given-name" })}${field("last_name", t("last_name"), { autocomplete: "family-name" })}${nav}</div>`;
  if (id === "phone") return `<div class="step">${q(t("q_phone"), t("you_why"))}${field("phone", t("phone"), { type: "tel", inputmode: "tel", autocomplete: "tel", hint: t("phone_hint") })}${choice("can_text", t("can_text"), yesno())}${nav}</div>`;
  if (id === "street") return `<div class="step">${q(t("q_street"))}${field("street", t("street"), { autocomplete: "street-address", hint: t("street_hint") })}${field("unit", t("unit"), { optional: true })}${nav}</div>`;
  if (id === "cityzip") {
    const cities = config.service_area.cities.map((c) => [c, c]).concat([["other", t("city_other")]]);
    return `<div class="step">${q(t("q_cityzip"), t("cityzip_note"))}${choice("city", "", cities)}${state.city === "other" ? field("city_other", t("city_other")) : ""}
      ${field("zip", t("zip"), { inputmode: "numeric", autocomplete: "postal-code", maxlength: 5 })}
      <p class="why" id="out-of-area" ${state.zip && digits(state.zip).length === 5 && !inArea() ? "" : "hidden"}>${esc(t("out_of_area"))}</p>${nav}</div>`;
  }
  if (id === "mail") return `<div class="step">${q(t("mail_same"))}${choice("mail_same", "", [["yes", t("mail_yes_stmt")], ["no", t("mail_no_stmt")]], { stack: true, big: true })}<p class="why">${esc(t("mail_why"))}</p>${nav}</div>`;
  if (id === "mail_addr") return `<div class="step">${q(t("q_mail_addr"))}${field("mail_street", t("mail_street"))}${field("mail_city", t("mail_city"), { optional: true })}${field("mail_zip", t("mail_zip"), { inputmode: "numeric", maxlength: 5, optional: true })}${nav}</div>`;
  if (id === "adults") return `<div class="step">${q(t("adults"))}${choice("adults", "", [1, 2, 3, 4, 5, 6].map((n) => [n, String(n)]))}${nav}</div>`;
  if (id === "adult_coats") return `<div class="step">${q(t("adult_coats"))}${choice("adult_coats", "", [["yes", t("coats_yes_stmt")], ["no", t("coats_no_stmt")]], { stack: true, big: true })}
      ${state.adult_coats === "yes" ? `<fieldset class="field ${errors.adult_coat_sizes ? "invalid" : ""}"><legend>${t("adult_coat_sizes")}</legend><div class="hint">${t("adult_coat_hint")}</div>
        <div class="choices">${state.adult_coat_sizes.map((sz, i) => `<label><input type="checkbox" checked data-action="rm-size" data-i="${i}"> ${esc(sz)}</label>`).join("")}</div>
        <div class="choices" style="margin-top:8px">${ADULT_SIZES.map((sz) => `<button type="button" class="btn btn-ghost" data-action="add-size" data-size="${sz}">+ ${sz}</button>`).join("")}</div>
        ${errors.adult_coat_sizes ? `<div class="msg" role="alert">${esc(errors.adult_coat_sizes)}</div>` : ""}</fieldset>` : ""}${nav}</div>`;
  if (id === "has_children") return `<div class="step">${q(t("q_has_children"))}${choice("has_children", "", [["yes", t("kids_yes_stmt")], ["no", t("kids_no_stmt")]], { stack: true, big: true })}<p class="why">${esc(t("children_why"))}</p>${nav}</div>`;
  const m = id.match(/^child:(\d+):(a|b|more)$/);
  if (m) {
    const i = +m[1]; const c = state.children[i] || blankChild();
    if (m[2] === "a") return `<div class="step">${q(t("q_child_a", i + 1))}
      ${field(`child_${i}_first_name`, t("child_first"), { value: c.first_name, autocomplete: "off" })}
      ${field(`child_${i}_age`, t("child_age"), { value: c.age, type: "number", inputmode: "numeric", hint: t("child_age_hint") })}
      ${i > 0 || state.children.length > 1 ? `<p><button type="button" class="btn btn-ghost" data-action="rm-child" data-i="${i}">${t("remove")}</button></p>` : ""}${nav}</div>`;
    if (m[2] === "b") return `<div class="step">${q(t("q_child_b", i + 1) + ": " + (c.first_name || ""))}
      ${choice(`child_${i}_sex`, t("child_sex"), [["boy", t("boy")], ["girl", t("girl")]], { value: c.sex })}
      ${choice(`child_${i}_coat`, t("child_coat"), yesno(), { value: c.coat })}
      ${c.coat === "yes" ? select(`child_${i}_coat_size`, t("child_coat_size"), CHILD_SIZES.map((sz) => [sz, sz]), { value: c.coat_size, optional: true }) : ""}${nav}</div>`;
    return `<div class="step">${q(t("q_child_more"))}${choice(`child_${i}_more`, "", [["yes", t("more_yes_stmt")], ["no", t("more_no_stmt")]], { value: c.more, stack: true, big: true })}${nav}</div>`;
  }
  if (id === "referral") {
    const opts = [["school", t("referral_school")], ["church", t("referral_church")], ["frc", t("referral_frc")], ["friend", t("referral_friend")], ["other", t("referral_other")], ["skip", t("referral_skip")]];
    return `<div class="step">${q(t("q_referral"))}${choice("referral_choice", "", opts, { stack: true })}${state.referral_choice === "other" ? field("referral", t("referral_other"), { optional: true }) : ""}${nav}</div>`;
  }
  if (id === "review") {
    const cityName = state.city === "other" ? state.city_other : state.city;
    const addr = `${state.street}${state.unit ? " " + state.unit : ""}, ${cityName} ${state.zip}`;
    const mail = state.mail_same === "yes" ? addr : [state.mail_street, state.mail_city, state.mail_zip].filter(Boolean).join(", ");
    const kids = state.has_children === "yes" ? state.children.map((c) => `${c.first_name}, ${c.age}, ${c.sex === "boy" ? t("boy") : c.sex === "girl" ? t("girl") : "?"}${c.coat === "yes" ? ", 🧥" : ""}`).join("<br>") : t("no_children");
    const row = (label, val, sid) => `<dt>${esc(label)} <a href="#" class="edit" data-action="goto" data-screen="${sid}">${t("edit")}</a></dt><dd>${val}</dd>`;
    return `<div class="step summary"><h1 class="q-title">${t("review_title")}</h1><dl>
      ${row(t("labels").name, esc(`${state.first_name} ${state.last_name}`), "name")}
      ${row(t("labels").phone, esc(state.phone), "phone")}
      ${row(t("labels").address, esc(addr), "street")}
      ${state.mail_same === "no" ? row(t("labels").mailing, esc(mail), "mail_addr") : ""}
      ${row(t("labels").adults, esc(`${state.adults}${state.adult_coat_sizes.length ? " · 🧥 " + state.adult_coat_sizes.join(", ") : ""}`), "adults")}
      ${row(t("labels").children, kids, "has_children")}</dl>
      ${state.has_children === "yes" ? `<p><button type="button" class="btn btn-ghost" data-action="add-child">+ ${t("add_child")}</button></p>` : ""}
      <div class="field ${errors.consent_all ? "invalid" : ""}"><div class="choices stack"><label><input type="checkbox" name="consent_all" ${state.consent_all ? "checked" : ""}> ${esc(t("confirm_all"))}</label></div>
      ${errors.consent_all ? `<div class="msg" role="alert">${t("required")}</div>` : ""}</div>
      ${nav}</div>`;
  }
  return `<div class="step"><p class="error">?</p></div>`;
}

function render() {
  if (speaking || player || (voiceAudio && !voiceAudio.paused)) stopSpeaking();
  const list = screens(); const total = list.length - 1; // welcome not counted
  const idx = stepIndex(); const pct = Math.round((idx / total) * 100);
  const canSpeak = ("speechSynthesis" in window) || Object.keys(audioManifest.files).length > 0;
  const hb = (icon, label) => `<span class="ico" aria-hidden="true">${icon}</span><span>${label}</span>`;
  const helpBar = `<div class="help-bar two">
    ${canSpeak ? `<button type="button" class="btn btn-ghost" data-action="speak" aria-pressed="${speaking}">${speaking ? hb("⏹", t("stop_reading")) : hb("🔊", t("read_aloud"))}</button>` : ""}
    <button type="button" class="btn btn-help" data-action="help-open" aria-expanded="${helpOpen}">${hb("🆘", t("help_btn"))}</button></div>`;
  const wa = config.help_phone.replace(/\D/g, "");
  const helpSheet = helpOpen ? `<div class="help-sheet" role="dialog" aria-modal="true" aria-label="${esc(t("help_btn"))}"><div class="help-sheet-inner">
    <p class="q-title">${t("help_sheet_title")}</p>
    <a class="btn btn-primary big" href="tel:${config.help_phone}">📞 ${t("help_call")}</a>
    <a class="btn btn-primary big" href="sms:${config.help_phone}">💬 ${t("help_text_msg")}</a>
    <a class="btn btn-primary big" href="https://wa.me/${wa}" target="_blank" rel="noopener">🟢 ${t("help_whatsapp")}</a>
    ${assistEnabled() ? `<button type="button" class="btn btn-ghost big" data-action="assist-toggle">❓ ${t("help_ask")}</button>` : ""}
    <button type="button" class="btn btn-ghost big" data-action="help-close">✖ ${t("help_close")}</button></div></div>` : "";
  const top = `<div class="apply-top"><a class="brand" href="${base}/${lang}/" aria-label="Truckee Community Cares"><img src="${base}/static/img/logo.png" alt="Truckee Community Cares" height="40"></a>
    <div class="lang-toggle" role="group" aria-label="Language"><button type="button" data-action="lang" data-lang="en" aria-pressed="${lang === "en"}"><span>English</span></button><button type="button" data-action="lang" data-lang="es" aria-pressed="${lang === "es"}"><span>Español</span></button></div></div>`;
  let body;
  if (view === "voice") {
    body = renderVoice();
  } else if (view === "done") {
    body = `<div class="done"><h1>✅ ${lastSupersedes ? t("done_updated_title") : previewMode ? t("preview_done_title") : t("done_title")}</h1>
      ${lastSupersedes ? `<p>${esc(t("done_updated_text", lastSupersedes))}</p>` : ""}<p>${t("done_code")}</p><div class="code">${previewMode ? "TEST · " : ""}${esc(doneId)}</div>
      <p>${t("done_text")}</p><p class="muted">${t("done_limited")}</p>
      <p><button class="btn btn-ghost" data-action="again">${t("done_again")}</button></p></div>`;
  } else if (!gate.open) {
    body = gate.reason === "stale"
      ? `<div class="closed"><h1>${t("stale_title")}</h1><p>${t("stale_text")}</p><button type="button" class="btn btn-primary btn-big" data-action="reload">${t("reload")}</button></div>`
      : `<div class="closed"><h1>${gate.reason === "not_open" ? t("not_open_title") : t("closed_title")}</h1>
      <p>${gate.reason === "not_open" ? esc(t("not_open_text", fmtDate(localToUtc(config.opens, config.timezone)))) : t("closed_text")}</p></div>`;
  } else {
    body = (idx > 0 ? `<div class="progress"><div class="bar"><div style="width:${pct}%"></div></div><div class="label">${t("step_of", idx, total)}</div></div>` : "") + renderStep();
    if (sendError) body += sendErrorCard();
  }
  const panel = assist.open ? `<section class="card assist" aria-label="${esc(t("assist_heading"))}"><h2>${t("assist_heading")} <button type="button" class="btn btn-ghost small" data-action="assist-toggle" style="float:right">${t("assist_close")}</button></h2>
    <p>${t("assist_intro")}</p>
    <div class="assist-log">${assist.history.map((m, i) => `<p class="${m.role}"><strong>${m.role === "user" ? "🙂" : "💡"}</strong> ${esc(m.content)}${m.role === "assistant" ? ` <button type="button" class="speak-inline" data-action="speak-text" data-i="${i}" aria-label="${esc(t("read_aloud"))}">🔊</button>` : ""}</p>`).join("")}${assist.busy ? `<p class="muted">${t("assist_thinking")}</p>` : ""}${assist.error ? `<p class="error">${t("assist_error")}</p>` : ""}</div>
    <form data-action="assist-ask" class="assist-form"><input id="assist-q" type="text" placeholder="${esc(t("assist_placeholder"))}" maxlength="500" autocomplete="off" ${assist.busy ? "disabled" : ""}><button class="btn btn-primary" ${assist.busy ? "disabled" : ""}>${t("assist_send")}</button></form></section>` : "";
  const banner = previewMode ? `<div class="announcement" role="status">${lang === "es" ? "MODO DE PRUEBA. Esta solicitud no cuenta. Las solicitudes reales abren el " : "PREVIEW MODE. This application does not count. Real applications open "}${esc(fmtDate(localToUtc(config.opens, config.timezone)))}.</div>` : "";
  const y = window.scrollY;
  root.innerHTML = top + banner + body + panel + helpBar + helpSheet;
  fitLabels();
  if (scrollTop) window.scrollTo(0, 0); else window.scrollTo(0, y);
  scrollTop = true;
  const firstErr = root.querySelector(".invalid input, .invalid select, [role=alert]");
  if (firstErr && Object.keys(errors).length) { firstErr.setAttribute("tabindex", "-1"); firstErr.scrollIntoView({ block: "center" }); firstErr.focus?.({ preventScroll: true }); }
}
function sendErrorCard() {
  if (sendReason === "stale") return `<div class="card"><p class="error">${t("stale_text")}</p><button class="btn btn-primary" data-action="reload">${t("reload")}</button></div>`;
  if (sendReason === "busy") return `<div class="card"><p class="error">${t("send_busy")}</p></div>`;
  return `<div class="card"><p class="error">${t("send_error")}</p><p>${t("send_error_help")}</p><button class="btn btn-primary" data-action="submit">${t("retry")}</button></div>`;
}

// Shrink label text so every button in a row fits with a little relief, whatever the
// language. Runs after each render and on resize. Buttons in one row share a size.
function fitLabels() {
  const FLOOR = 0.75;   // never below 75% of the CSS size (about 11px); wrap instead
  for (const row of root.querySelectorAll(".help-bar, .lang-toggle")) {
    const labels = [...row.querySelectorAll(".btn > span:not(.ico), button > span:not(.ico)")];
    if (!labels.length) continue;
    const relief = 4;
    let scale = 1;
    const avail = new Map();
    for (const l of labels) {
      l.style.fontSize = ""; l.style.whiteSpace = "nowrap";
      const btn = l.closest(".btn, button"); const cs = getComputedStyle(btn);
      const a = btn.clientWidth - relief - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
      avail.set(l, a);
      if (l.scrollWidth > a && a > 0) scale = Math.min(scale, a / l.scrollWidth);
    }
    if (scale >= 1) continue;
    scale = Math.max(FLOOR, scale);
    for (const l of labels) {
      l.style.fontSize = `${scale * parseFloat(getComputedStyle(l).fontSize)}px`;
      if (l.scrollWidth > avail.get(l)) l.style.whiteSpace = "normal";  // still too long: two lines
    }
  }
}
let fitTimer;
window.addEventListener("resize", () => { clearTimeout(fitTimer); fitTimer = setTimeout(fitLabels, 100); });

// ---------- state updates ----------
// Side effects of a decision, applied when the person taps Next on that screen.
function afterAnswer(id) {
  if (id === "has_children") {
    if (state.has_children === "yes") { state.no_children = false; if (!state.children.length) state.children.push(blankChild()); }
    else { state.no_children = true; state.children = []; }
  }
  const m = id.match(/^child:(\d+):more$/);
  if (m) {
    const i = +m[1];
    if (state.children[i].more === "yes") { if (!state.children[i + 1]) state.children.push(blankChild()); }
    else state.children.splice(i + 1);
  }
  if (id === "adult_coats" && state.adult_coats === "no") state.adult_coat_sizes = [];
  if (id === "mail" && state.mail_same === "yes") { state.mail_street = ""; state.mail_city = ""; state.mail_zip = ""; }
  if (id === "referral") state.referral = state.referral_choice === "other" ? state.referral : (state.referral_choice === "skip" ? "" : state.referral_choice);
}
function readInputs() {
  root.querySelectorAll("input, select, textarea").forEach((el) => {
    const n = el.name; if (!n) return;
    const m = n.match(/^child_(\d+)_(.+)$/);
    if (m) { const c = state.children[+m[1]]; if (!c) return; if (el.type === "radio") { if (el.checked) c[m[2]] = el.value; } else c[m[2]] = el.value; return; }
    if (el.type === "checkbox") state[n] = el.checked;
    else if (el.type === "radio") { if (el.checked) state[n] = el.value; }
    else state[n] = el.value;
  });
  saveDraft();
}

let view = "form"; let doneId = ""; let sendError = false; let sending = false; let speaking = false; let helpOpen = false;
let assist = { open: false, history: [], busy: false, error: false };
let audioManifest = { files: {} };   // site/static/audio/manifest.json, pre-rendered screens
let player = null;                    // the one <audio> element in use
const ttsEnabled = () => !!(config && config.tts);
let freeform = { text: "", busy: false, error: false, short: false, missing: null, summary: "" };
let rec = { state: "idle", recorder: null, chunks: [], stream: null, error: "" };  // idle | recording | transcribing
let voice = null;          // voice mode state, see startVoice()
let voiceAudio = null;     // one <audio> element unlocked by the entry tap; reused for every playback
const voiceEnabled = () => sttEnabled() && assistEnabled() && ttsEnabled();
const sttEnabled = () => !!(config && config.stt) && !!navigator.mediaDevices?.getUserMedia && "MediaRecorder" in window;
const assistEnabled = () => !!(config && config.assist);

// Read the current screen aloud. Pre-rendered audio (a real voice, rendered at build
// time by bin/build-audio.mjs) when it exists; otherwise the device's own speech engine.
function stopSpeaking() {
  voiceGen += 1;
  if (player) { player.pause(); player = null; }
  if (voiceAudio) { voiceAudio.onended = voiceAudio.onerror = null; voiceAudio.pause(); }
  if (window.speechSynthesis) window.speechSynthesis.cancel();
  speaking = false;
  const b = root.querySelector('[data-action="speak"]'); if (b) { b.innerHTML = `<span class="ico" aria-hidden="true">🔊</span><span>${esc(t("read_aloud"))}</span>`; b.setAttribute("aria-pressed", "false"); }
}
function markSpeaking() {
  speaking = true;
  const b = root.querySelector('[data-action="speak"]'); if (b) { b.innerHTML = `<span class="ico" aria-hidden="true">⏹</span><span>${esc(t("stop_reading"))}</span>`; b.setAttribute("aria-pressed", "true"); }
}
function playUrl(url, onend) {
  player = new Audio(url);
  player.onended = () => { player = null; if (onend) onend(); else stopSpeaking(); };
  player.onerror = () => { player = null; stopSpeaking(); };
  markSpeaking();
  player.play().catch(() => stopSpeaking());
}
function synthSpeak(text, onend) {
  const synth = window.speechSynthesis; if (!synth || !text) { if (onend) onend(); else stopSpeaking(); return; }
  const u = new SpeechSynthesisUtterance(text);
  u.lang = lang === "es" ? "es-MX" : "en-US"; u.rate = 0.95;
  const voices = synth.getVoices();
  const voice = voices.find((v) => v.lang.toLowerCase().startsWith(lang === "es" ? "es-mx" : "en-us")) || voices.find((v) => v.lang.toLowerCase().startsWith(lang));
  if (voice) u.voice = voice;
  u.onend = () => { if (onend) onend(); else stopSpeaking(); };
  markSpeaking(); synth.speak(u);
}
function currentScreenKey() {
  if (view === "done") return "done";
  if (!gate.open) return gate.reason === "not_open" ? "not_open" : "closed";
  const m = cur.match(/^child:\d+:(a|b|more)$/);
  return m ? `child_${m[1]}` : cur;
}
function speakPage() {
  if (speaking) { stopSpeaking(); return; }
  const key = currentScreenKey();
  const mode = config.mode && config.mode !== "pickup" ? config.mode : "";
  const name = (mode && audioManifest.files[`${lang}/${key}@${mode}`]) ? `${lang}/${key}@${mode}` : `${lang}/${key}`;
  const stepEl = root.querySelector(".step, .done, .closed");
  const domText = stepEl ? [...stepEl.querySelectorAll("h1, h2, p, li, label, legend, .hint, dt, dd")].map((n) => n.textContent.trim()).filter(Boolean).join(". ") : "";
  // Dynamic pieces: a board notice or the preview warning first, the confirmation code last.
  const head = [(config.announcement || {})[lang] || "", previewMode ? t("preview_spoken") : ""].filter(Boolean).join(". ");
  const tail = view === "done" ? doneId.split("").join(" ") : "";
  const playMain = () => { if (audioManifest.files[name]) playUrl(`${base}/static/audio/${name}.mp3`, tail ? () => speakTail(tail) : null); else synthSpeak(domText); };
  if (head) speakTail(head, playMain); else playMain();
}
// Dynamic text after a recording: server voice when the API has it, else the device voice.
async function speakTail(text, onend) {
  const gen = voiceGen;
  if (ttsEnabled()) {
    try {
      const r = await fetch(`${config.api_base}/api/tts`, { method: "POST", headers: apiHeaders({ "content-type": "application/json" }), body: JSON.stringify({ lang, text }) });
      if (gen !== voiceGen) return;
      if (r.ok) { playUrl(URL.createObjectURL(await r.blob()), onend); return; }
    } catch (e) { /* fall through */ }
  }
  if (gen !== voiceGen) return;
  synthSpeak(text, onend);
}
// Speak a piece of dynamic text (a help answer) with the server voice, else the device voice.
async function speakText(text) {
  if (speaking) { stopSpeaking(); return; }
  speakTail(text);
}

root.addEventListener("click", async (ev) => {
  const el = ev.target.closest("[data-action]"); if (!el) return;
  const a = el.dataset.action;
  if (a === "assist-ask" || a === "check-form") return; // the submit handler owns these; re-rendering here would drop the input
  if (el.tagName === "A") ev.preventDefault();
  if (a === "speak") { speakPage(); return; }
  if (a === "speak-text") { speakText(assist.history[+el.dataset.i]?.content || ""); return; }
  if (a === "assist-toggle") { readInputs(); helpOpen = false; assist.open = !assist.open; render(); if (assist.open) root.querySelector("#assist-q")?.focus(); return; }
  if (a === "help-open") { readInputs(); helpOpen = true; scrollTop = false; render(); return; }
  if (a === "help-close") { helpOpen = false; scrollTop = false; render(); return; }
  if (a === "freeform") { const ta = root.querySelector("#freeform"); freeform.text = ta ? ta.value : ""; await runFreeform(); return; }
  if (a === "record") { rec.prompt = ""; await toggleRecording(); return; }
  if (a === "voice-start") { readInputs(); startVoice(); return; }
  if (a === "voice-replay") { voiceAsk(currentQuestion().key); return; }
  if (a === "voice-begin") { voiceBegin(); return; }
  if (a === "voice-talk") { await voiceTalk(); return; }
  if (a === "voice-ok") { voiceNext(); return; }
  if (a === "voice-again") { voiceRepeat(); return; }
  if (a === "voice-skip") { voiceNext(true); return; }
  if (a === "voice-review") { leaveVoice(); return; }
  if (a === "voice-send") { state.consent_all = true; state.remember = false; await submit(); return; }
  if (a === "reload") { location.reload(); return; }
  if (a === "lang") {
    readInputs();
    const wasSpeaking = speaking || (voiceAudio && !voiceAudio.paused);
    const inVoice = view === "voice";
    setLang(el.dataset.lang);
    if (inVoice && voice) {
      if (voice.phase === "intro") voicePlay(`${base}/static/audio/${lang}/voice_intro.mp3`, () => {});
      else if (voice.phase === "confirm") { voice.readback = readbackFor(currentQuestion()); render(); voiceSay(voice.readback); }
      else if (voice.phase === "ask") voiceAsk(currentQuestion().key);
    } else if (wasSpeaking) speakPage();
    return;
  }
  readInputs();
  if (a === "next") {
    errors = validate(cur); if (Object.keys(errors).length) return render();
    afterAnswer(cur);
    const list = screens(); const i = list.indexOf(cur);
    if (editReturn) {
      // Back to the review unless the change opened screens that still need an answer.
      // Child screens run through to "another child?" so a child can be added or removed.
      const nxt = list[i + 1];
      const pending = list.slice(i + 1, list.indexOf("review")).find((sid) => Object.keys(validate(sid)).length);
      if (pending) cur = pending;
      else if (nxt && nxt.startsWith("child:") && (cur === "has_children" || cur.startsWith("child:"))) cur = nxt;
      else { cur = "review"; editReturn = null; }
    } else cur = list[Math.min(i + 1, list.length - 1)];
  }
  if (a === "back") { errors = {}; if (editReturn) { cur = "review"; editReturn = null; } else { const list = screens(); cur = list[Math.max(list.indexOf(cur) - 1, 0)]; } }
  if (a === "goto") { errors = {}; editReturn = cur === "review" ? "review" : null; cur = screens().includes(el.dataset.screen) ? el.dataset.screen : "review"; }
  if (a === "add-child") {
    errors = {}; editReturn = "review";
    if (state.children.length) state.children[state.children.length - 1].more = "yes";
    state.children.push(blankChild()); state.has_children = "yes"; state.no_children = false;
    cur = `child:${state.children.length - 1}:a`;
  }
  if (a === "rm-child") { const i = +el.dataset.i; state.children.splice(i, 1); if (!state.children.length) { state.has_children = ""; cur = "has_children"; } else { const j = Math.min(i, state.children.length - 1); state.children[j].more = j === state.children.length - 1 ? "no" : "yes"; cur = `child:${j}:a`; } }
  if (a === "add-size") { state.adult_coat_sizes.push(el.dataset.size); }
  if (a === "rm-size") { state.adult_coat_sizes.splice(+el.dataset.i, 1); }
  if (a === "prefill") {
    const rem = loadRemembered();
    if (rem) {
      state = { ...blankState(), ...rem.state, consent_all: false, remember: true };
      // Children are a year older per season since the answers were saved.
      const years = Math.max(0, Math.round((Date.now() - Date.parse(rem.saved || 0)) / (365.25 * 24 * 3600 * 1000)));
      if (years) state.children = state.children.map((c) => ({ ...c, age: c.age === "" ? "" : String(Number(c.age) + years), coat_size: "" }));
    }
    prefilled = true; cur = "helper";
  }
  if (a === "fresh") { try { localStorage.removeItem(REMEMBER_KEY); } catch (e) {} prefilled = true; cur = "helper"; }
  if (a === "edit-sent") {
    const sent = loadSent();
    if (sent) { state = { ...blankState(), ...sent.state, consent_all: false }; supersedes = sent.id; previewMode = !!sent.preview || previewMode; cur = "review"; }
  }
  if (a === "new-family") { clearSent(); state = blankState(); supersedes = ""; cur = "helper"; }
  if (a === "check-open") {
    check = { open: true, code: "", busy: false, result: null, error: "" }; scrollTop = false; render();
    root.querySelector("#check-code")?.focus();
    return;
  }
  if (a === "edit-copy") {
    copyError = ""; el.disabled = true; el.textContent = t("opening_copy");
    try {
      const saved = await openSelfCopy(el.dataset.code);
      state = { ...blankState(), ...saved, consent_all: false }; supersedes = el.dataset.code; editReturn = null; cur = "review"; saveDraft(); render();
    } catch (e) { console.error(e); copyError = t("st_copy_failed"); scrollTop = false; render(); }
    return;
  }
  if (a === "forget-code") {
    forgetCode(el.dataset.code); if (loadSent()?.id === el.dataset.code) clearSent(); sentStatus = null; scrollTop = false; render();
    const nxt = loadCodes()[0]; if (nxt) fetchStatus(nxt.id).then((st) => { if (st && cur === "welcome") { sentStatus = st; scrollTop = false; render(); } }).catch(() => {});
    return;
  }
  if (a === "redo") { clearSent(); state = blankState(); supersedes = String(el.dataset.code || "").toUpperCase(); check = { open: false, code: "", busy: false, result: null, error: "" }; cur = "helper"; }
  if (a === "again") { clearSent(); state = blankState(); cur = "welcome"; view = "form"; doneId = ""; prefilled = true; editReturn = null; supersedes = ""; lastSupersedes = ""; }
  if (a === "submit") { errors = validate("review"); if (Object.keys(errors).length) return render(); await submit(); return; }
  errors = {}; saveDraft(); render();
});
root.addEventListener("submit", async (ev) => {
  const cf = ev.target.closest('[data-action="check-form"]');
  if (cf) {
    ev.preventDefault();
    const code = (root.querySelector("#check-code")?.value || "").trim().toUpperCase().replace(/\s+/g, "");
    check.code = code; check.error = ""; check.result = null;
    if (!/^TCC-[A-Z0-9]{2}-[A-Z0-9]{5}$/.test(code)) { check.error = t("check_not_found"); scrollTop = false; render(); return; }
    check.busy = true; scrollTop = false; render();
    try { const st = await fetchStatus(code); if (!st) check.error = t("check_not_found"); else { rememberCode(st.id, st.season, st.created_at); clearSent(); sentStatus = st; check.open = false; } }
    catch (e) { check.error = t("send_error"); }
    check.busy = false; scrollTop = false; render();
    return;
  }
  const f = ev.target.closest('[data-action="assist-ask"]'); if (!f) return;
  ev.preventDefault();
  const q = root.querySelector("#assist-q").value.trim(); if (!q || assist.busy) return;
  readInputs();
  assist.history.push({ role: "user", content: q }); assist.busy = true; assist.error = false; render();
  try {
    const r = await fetch(`${config.api_base}/api/help`, { method: "POST", headers: apiHeaders({ "content-type": "application/json" }),
      body: JSON.stringify({ lang, question: q, history: assist.history.slice(0, -1).slice(-8) }) });
    if (!r.ok) throw new Error(r.status);
    const { answer } = await r.json();
    assist.history.push({ role: "assistant", content: answer });
  } catch (e) { assist.error = true; }
  assist.busy = false; render(); root.querySelector("#assist-q")?.focus();
});


// ---------- Recording overlay ----------
// While the microphone is open, a full-screen sheet shows a live level meter, a
// countdown, and one big Done button, so a person knows the phone is listening.
const REC_LIMIT_MS = 90000;
let overlay = { el: null, raf: 0, timer: 0, ctx: null, analyser: null, quietSince: 0, startedAt: 0, prompt: "" };

function openOverlay(stream, prompt) {
  closeOverlay();
  const el = document.createElement("div");
  el.className = "rec-overlay"; el.setAttribute("role", "dialog"); el.setAttribute("aria-modal", "true"); el.setAttribute("aria-label", t("rec_listening"));
  el.innerHTML = `<div class="rec-sheet">
    <p class="rec-title" aria-live="polite">🎤 ${t("rec_listening")}</p>
    ${prompt ? `<p class="rec-prompt">${esc(prompt)}</p>` : ""}
    <canvas class="rec-meter" width="320" height="110" aria-hidden="true"></canvas>
    <p class="rec-hint">${t("rec_hint")}</p>
    <p class="rec-time"><span class="rec-clock">0:00</span><span class="rec-left"></span></p>
    <button type="button" class="btn btn-primary rec-done">✅ ${t("rec_done")}</button>
    <button type="button" class="btn btn-ghost rec-cancel">✖ ${t("rec_cancel")}</button></div>`;
  document.body.appendChild(el);
  document.body.classList.add("rec-open");
  el.querySelector(".rec-done").addEventListener("click", () => { if (rec.state === "recording") { showProcessing(); rec.recorder.stop(); } });
  el.querySelector(".rec-cancel").addEventListener("click", cancelRecording);
  overlay = { ...overlay, el, startedAt: performance.now(), quietSince: 0, prompt };
  el.querySelector(".rec-done").focus();
  startMeter(stream, el.querySelector(".rec-meter"));
  const clock = el.querySelector(".rec-clock"), left = el.querySelector(".rec-left"), hint = el.querySelector(".rec-hint");
  overlay.timer = setInterval(() => {
    const ms = performance.now() - overlay.startedAt;
    const sec = Math.floor(ms / 1000);
    clock.textContent = `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
    const remaining = Math.max(0, Math.ceil((REC_LIMIT_MS - ms) / 1000));
    left.textContent = remaining <= 20 ? " · " + t("rec_limit", remaining) : "";
    if (overlay.quietSince && performance.now() - overlay.quietSince > 3500) { hint.textContent = t("rec_quiet"); hint.classList.add("error"); }
    else { hint.textContent = t("rec_hint"); hint.classList.remove("error"); }
  }, 250);
}
function showProcessing() {
  if (!overlay.el) return;
  stopMeter();
  overlay.el.querySelector(".rec-sheet").innerHTML = `<p class="rec-title">${t("rec_processing")}</p><div class="rec-spinner" aria-hidden="true"></div>
    <button type="button" class="btn btn-ghost rec-cancel">✖ ${t("rec_cancel")}</button>`;
  overlay.el.querySelector(".rec-cancel").addEventListener("click", cancelRecording);
}
// Stop everything about the current recording and tell the caller it was cancelled.
function cancelRecording() {
  if (rec.state === "recording") { rec.cancelled = true; rec.recorder.stop(); return; }
  if (rec.state === "transcribing") { rec.cancelled = true; if (rec.abort) rec.abort.abort(); return; }
  closeOverlay();
}
function closeOverlay() {
  stopMeter();
  clearInterval(overlay.timer); overlay.timer = 0;
  if (overlay.el) { overlay.el.remove(); overlay.el = null; }
  document.body.classList.remove("rec-open");
}
function startMeter(stream, canvas) {
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!AC || !canvas) return;
  try {
    const ctx = new AC(); if (ctx.state === "suspended") ctx.resume().catch(() => {}); const src = ctx.createMediaStreamSource(stream); const analyser = ctx.createAnalyser();
    analyser.fftSize = 512; analyser.smoothingTimeConstant = 0.6; src.connect(analyser);
    overlay.ctx = ctx; overlay.analyser = analyser;
    const data = new Uint8Array(analyser.fftSize); const g = canvas.getContext("2d");
    const bars = 24; const hist = new Array(bars).fill(0);
    const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const brand = getComputedStyle(document.documentElement).getPropertyValue("--brand-dark").trim() || "#4d6b53";
    const draw = () => {
      analyser.getByteTimeDomainData(data);
      let sum = 0; for (let i = 0; i < data.length; i++) { const v = (data[i] - 128) / 128; sum += v * v; }
      const rms = Math.sqrt(sum / data.length);
      const level = Math.min(1, rms * 6);
      if (level < 0.03) { if (!overlay.quietSince) overlay.quietSince = performance.now(); } else overlay.quietSince = 0;
      hist.push(level); hist.shift();
      g.clearRect(0, 0, canvas.width, canvas.height);
      const w = canvas.width / bars;
      for (let i = 0; i < bars; i++) {
        const h = Math.max(4, hist[i] * canvas.height);
        g.fillStyle = brand; g.globalAlpha = 0.35 + 0.65 * (i / bars);
        g.fillRect(i * w + 3, (canvas.height - h) / 2, w - 6, h);
      }
      g.globalAlpha = 1;
      overlay.raf = reduce ? setTimeout(draw, 250) : requestAnimationFrame(draw);
    };
    draw();
  } catch (e) { /* no meter, recording still works */ }
}
function stopMeter() {
  if (overlay.raf) { cancelAnimationFrame(overlay.raf); clearTimeout(overlay.raf); overlay.raf = 0; }
  if (overlay.ctx) { try { overlay.ctx.close(); } catch (e) {} overlay.ctx = null; overlay.analyser = null; }
}

// Record with the browser's own recorder, transcribe on the server, then fill the form.
function pickMime() {
  for (const m of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"]) if (MediaRecorder.isTypeSupported(m)) return m;
  return "";
}
async function toggleRecording(onText) {
  if (rec.state === "recording") { showProcessing(); rec.recorder.stop(); return; }
  if (rec.state !== "idle") return;   // opening or transcribing: ignore a second tap
  stopSpeaking();
  const prompt = rec.prompt || "";
  rec = { state: "opening", recorder: null, chunks: [], stream: null, error: "", onText: onText || null, cancelled: false, prompt };
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
  catch (e) { rec = { state: "idle", recorder: null, chunks: [], stream: null, error: t("mic_denied"), prompt: "" }; render(); return; }
  if (rec.state !== "opening") { stream.getTracks().forEach((tr) => tr.stop()); return; }  // cancelled while opening
  const mime = pickMime();
  let recorder;
  try { recorder = new MediaRecorder(stream, mime ? { mimeType: mime, audioBitsPerSecond: 32000 } : { audioBitsPerSecond: 32000 }); }
  catch (e) { stream.getTracks().forEach((tr) => tr.stop()); rec = { state: "idle", recorder: null, chunks: [], stream: null, error: t("mic_denied"), prompt: "" }; render(); return; }
  rec = { state: "recording", recorder, chunks: [], stream, error: "", onText: onText || null, cancelled: false, prompt, abort: null };
  recorder.ondataavailable = (e) => { if (e.data.size) rec.chunks.push(e.data); };
  recorder.onstop = async () => {
    stream.getTracks().forEach((tr) => tr.stop());
    const finish = (error) => { rec = { state: "idle", recorder: null, chunks: [], stream: null, error, prompt: "" }; closeOverlay(); };
    if (rec.cancelled) { const cb = rec.onText; finish(""); if (cb) { await cb(null); return; } render(); return; }
    showProcessing();
    const blob = new Blob(rec.chunks, { type: recorder.mimeType || "audio/webm" });
    rec.state = "transcribing"; rec.abort = new AbortController(); render();
    const tid = setTimeout(() => rec.abort && rec.abort.abort(), 30000);
    let text = false;   // false = server or network trouble, "" = nothing heard
    try {
      const ext = /mp4/.test(blob.type) ? "m4a" : /ogg/.test(blob.type) ? "ogg" : "webm";
      const fd = new FormData(); fd.append("file", blob, `speech.${ext}`); fd.append("lang", lang);
      const r = await fetch(`${config.api_base}/api/transcribe`, { method: "POST", body: fd, headers: apiHeaders(), signal: rec.abort.signal });
      if (r.status === 429) text = "busy";
      else if (r.ok) text = String((await r.json()).text || "");
    } catch (e) { text = rec.cancelled ? null : false; }
    finally { clearTimeout(tid); }
    const cb = rec.onText;
    if (text === null) { finish(""); if (cb) { await cb(null); return; } render(); return; }
    if (text === false || text === "busy") { finish(text === "busy" ? t("speak_busy") : t("speak_error")); if (cb) { await cb(false); return; } render(); return; }
    if (!text) { finish(t("speak_error")); if (cb) { await cb(""); return; } render(); return; }
    finish("");
    if (cb) { await cb(text); return; }
    const ta = root.querySelector("#freeform");
    freeform.text = ((ta ? ta.value : freeform.text).trim() + " " + text).trim();
    await runFreeform();
  };
  recorder.start();
  openOverlay(stream, prompt);
  render();
  // Safety stop at the limit so a forgotten recording does not run forever.
  setTimeout(() => { if (rec.recorder === recorder && recorder.state === "recording") { showProcessing(); recorder.stop(); } }, REC_LIMIT_MS);
}

// People say sizes in words, in either language. Map them to the size chips.
function normalizeSize(v) {
  const x = String(v || "").trim().toLowerCase();
  const table = [[/^(3xl|xxxl|triple)/, "3XL"], [/^(2xl|xxl|doble)/, "2XL"], [/^(xl|extra ?grande|extra ?large)/, "XL"], [/^(l|large|grande)/, "L"],
    [/^(m|medium|median[ao])/, "M"], [/^(s|small|chic[ao]|peque[ñn][ao])/, "S"]];
  for (const [re, out] of table) if (re.test(x)) return out;
  return String(v || "").toUpperCase().slice(0, 4);
}

function applyExtracted(f, { replaceChildren = true } = {}) {
  const cities = config.service_area.cities.map((c) => c.toLowerCase());
  const keep = (v, cur) => (v === "" || v === 0 || v === null || v === undefined || (Array.isArray(v) && !v.length)) ? cur : v;
  state.first_name = keep(f.first_name, state.first_name); state.last_name = keep(f.last_name, state.last_name);
  state.phone = keep(f.phone, state.phone); state.other_adult = keep(f.other_adult, state.other_adult);
  if (f.phone && f.can_text === false) state.can_text = "no"; else if (f.phone) state.can_text = state.can_text || "yes";
  state.street = keep(f.street, state.street); state.unit = keep(f.unit, state.unit); state.zip = keep(f.zip, state.zip);
  if (f.city) { const i = cities.indexOf(f.city.toLowerCase()); state.city = i >= 0 ? config.service_area.cities[i] : "other"; state.city_other = i >= 0 ? "" : f.city; }
  if (f.mail_street) { state.mail_same = "no"; state.mail_street = f.mail_street; }
  if (f.adults) state.adults = String(f.adults);
  if (f.adult_coat_sizes?.length) { state.adult_coat_sizes = f.adult_coat_sizes.map(normalizeSize); state.adult_coats = "yes"; }
  if (f.children?.length) {
    const kids = f.children
      .map((c) => ({ ...blankChild(), first_name: (c.first_name || "").trim(), age: (c.age === null || c.age === undefined || c.age === "") ? "" : String(c.age), sex: c.sex || "", coat: c.coat ? "yes" : "no" }))
      .filter((c) => c.first_name || c.age !== "");
    state.children = replaceChildren ? kids : state.children.concat(kids); state.no_children = false; state.has_children = "yes";
    state.children.forEach((c, i) => { c.more = i < state.children.length - 1 ? "yes" : "no"; });
  }
  if (f.want_food) state.want_food = true; if (f.want_coats) state.want_coats = true;
}

async function extractText(text, question) {
  const r = await fetch(`${config.api_base}/api/extract`, { method: "POST", headers: apiHeaders({ "content-type": "application/json" }), body: JSON.stringify({ lang, text, question }) });
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

async function runFreeform() {
  if (freeform.busy) return;
  if (freeform.text.trim().length < 10) { freeform.short = true; freeform.error = false; freeform.missing = null; render(); return; }
  freeform.busy = true; freeform.short = false; freeform.error = false; freeform.missing = null; render();
  try {
    const { fields: f, missing } = await extractText(freeform.text);
    if (!f) throw new Error("no fields");
    applyExtracted(f);
    freeform.missing = missing || []; prefilled = true; cur = "helper"; saveDraft();
  } catch (e) { console.error(e); freeform.error = true; }
  freeform.busy = false; render();
}

root.addEventListener("change", (ev) => {
  // Re-render for choices that reveal or hide other fields. These fire after the tap
  // completes, and render() keeps the scroll position, so nothing jumps.
  const n = ev.target.name || "";
  if (n === "helper" && ev.target.value === "yes" && ev.target.checked) state.remember = false;
  if (["city", "adult_coats", "referral_choice"].includes(n) || /^child_\d+_coat$/.test(n)) { readInputs(); scrollTop = false; render(); }
});
root.addEventListener("input", (ev) => {
  if ((ev.target.name || "") !== "zip") return;
  const hint = root.querySelector("#out-of-area"); if (!hint) return;
  const z = digits(ev.target.value);
  hint.hidden = !(z.length === 5 && !(config.service_area.zips.includes(z) || config.service_area.cities.map((c) => c.toLowerCase()).includes((state.city || "").toLowerCase())));
});
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && rec.state !== "idle") cancelRecording(); });
window.addEventListener("pageshow", (ev) => { if (ev.persisted) location.reload(); });
window.addEventListener("pagehide", () => { if (rec.state !== "idle") cancelRecording(); });

function payload() {
  const cityName = state.city === "other" ? state.city_other : state.city;
  return {
    version: 1, season: previewMode ? "preview" : config.season, lang, submitted_at: new Date().toISOString(),
    helper: state.helper === "yes" ? { name: state.helper_name.trim(), phone: digits(state.helper_phone), org: state.helper_org.trim() } : null,
    applicant: { first_name: state.first_name.trim(), last_name: state.last_name.trim(), phone: digits(state.phone), can_text: state.can_text === "yes",
      other_phone: digits(state.other_phone), email: state.email.trim(), other_adult: state.other_adult.trim(), contact_lang: state.contact_lang },
    address: { street: state.street.trim(), unit: state.unit.trim(), city: cityName, zip: digits(state.zip), in_area: inArea() },
    mailing: state.mail_same === "yes" ? null : { street: state.mail_street.trim(), city: state.mail_city.trim(), zip: digits(state.mail_zip) },
    household: { adults: Number(state.adults), adult_coat_sizes: state.adult_coat_sizes },
    children: (state.has_children === "yes" ? state.children : []).map((c) => ({ first_name: c.first_name.trim(), age: Number(c.age), sex: c.sex,
      school: c.school === "other" ? c.school_other.trim() : c.school, coat: c.coat === "yes", coat_size: c.coat_size })),
    programs: { food: true, toys: state.has_children === "yes" && state.children.length > 0, coats: state.adult_coat_sizes.length > 0 || state.children.some((c) => c.coat === "yes") },
    referral: (state.referral || "").trim(), notes: (state.notes || "").trim(),
  };
}

// Every path to a send goes through here, so this is the one place that checks the form.
function firstInvalidStep() {
  for (const id of screens().slice(1)) if (Object.keys(validate(id)).length) return id;
  return null;
}
async function submit() {
  if (sending) return;
  const bad = firstInvalidStep();
  if (bad) {
    // Something required is missing: land on that step with the field marked.
    stopSpeaking(); closeOverlay();
    view = "form"; voice = null; cur = bad; errors = validate(bad); render();
    return;
  }
  if (!previewMode) {
    const g = computeGate();
    if (!g.open) { const fromVoice = view === "voice"; gate = g; view = "form"; voice = null; render(); if (fromVoice) voiceSay(t("closed_text")); return; }
  }
  sending = true; sendError = false; sendReason = "";
  const btn = root.querySelector('[data-action="submit"], [data-action="voice-send"]'); if (btn) { btn.disabled = true; btn.textContent = t("sending"); }
  try {
    if (!config.recipients || !config.recipients.length) throw new Error("no recipients");
    const enc = new Encrypter();
    for (const r of config.recipients) enc.addRecipient(r);
    const ciphertext = armor.encode(await enc.encrypt(JSON.stringify(payload())));
    const selfKey = randomKey();
    const self_copy = await makeSelfCopy(selfKey);
    const res = await fetch(`${config.api_base}/api/apply`, { method: "POST", headers: apiHeaders({ "content-type": "application/json" }),
      body: JSON.stringify({ season: previewMode ? "preview" : config.season, lang, ciphertext, supersedes: supersedes || undefined, self_copy }) });
    if (!res.ok) {
      let err = ""; try { err = (await res.json()).error || ""; } catch (e) {}
      if (res.status === 403 && (err === "closed" || err === "not_open")) { gate = { open: false, reason: err }; view = "form"; voice = null; render(); return; }
      if (res.status === 400 && err === "wrong_season") { sendReason = "stale"; throw new Error(err); }
      if (res.status === 400 && err === "bad_supersedes") { supersedes = ""; clearSent(); sending = false; return submit(); }
      if (res.status === 429) {
        sendReason = "busy";
        setTimeout(() => {
          if (sendReason !== "busy") return;
          // Retry only if the person is still on the send screen and not typing; otherwise leave the manual button.
          const onSendScreen = view === "voice" ? (voice && voice.phase === "summary") : cur === "review";
          const typing = document.activeElement && /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
          sendError = false; sendReason = "";
          if (onSendScreen && !typing) { readInputs(); submit(); } else { sendError = true; render(); }
        }, 30000 + Math.random() * 15000);
        throw new Error(err || "busy");
      }
      throw new Error(`HTTP ${res.status}`);
    }
    const data = await res.json();
    doneId = data.id;
    lastSupersedes = supersedes; supersedes = ""; sentStatus = null;
    rememberCode(doneId, previewMode ? "preview" : config.season, new Date().toISOString(), selfKey);
    clearSent();
    try { localStorage.removeItem(REMEMBER_KEY); } catch (e) {}
    try { sessionStorage.removeItem(DRAFT_KEY); } catch (e) {}
    const fromVoice = view === "voice";
    view = "done"; voice = null;
    if (fromVoice) speakAfter = "done";   // the person cannot read the code: say it, on the unlocked element
  } catch (e) {
    console.error(e); sendError = true;
    if (view === "voice") speakAfter = sendReason === "busy" ? t("send_busy") : sendReason === "stale" ? t("stale_text") : t("send_error");
  } finally {
    sending = false; render();
    if (speakAfter === "done") speakDoneVoice();
    else if (speakAfter) voiceSay(speakAfter);
    speakAfter = "";
  }
}
let speakAfter = "";
// After a voice-mode send: the done recording, then the confirmation code letter by letter,
// both through the element the entry tap unlocked (a fresh Audio() may not play on iOS).
function speakDoneVoice() {
  const mode = config.mode && config.mode !== "pickup" ? config.mode : "";
  const name = (mode && audioManifest.files[`${lang}/done@${mode}`]) ? `${lang}/done@${mode}` : `${lang}/done`;
  const code = [previewMode ? t("preview_spoken") : "", doneId.split("").join(" ")].filter(Boolean).join(". ");
  if (audioManifest.files[name]) voicePlay(`${base}/static/audio/${name}.mp3`, () => voiceSay(code));
  else voiceSay(t("done_title") + ". " + code);
}


// ---------- Voice mode ----------
// A person who cannot read the form answers out loud. Each question is a pre-rendered
// recording; the answer is transcribed and extracted on the server; the readback is
// spoken with the same voice. Only one thing on screen at a time, all buttons huge.
const VOICE_QS = [
  { id: "who", fields: ["first_name", "last_name", "phone", "can_text"] },
  { id: "home", fields: ["street", "unit", "city", "city_other", "zip"] },
  { id: "mail", fields: ["mail_same", "mail_street", "mail_city", "mail_zip"] },
  { id: "adults", fields: ["adults", "adult_coats", "adult_coat_sizes"] },
  { id: "children", fields: ["children", "no_children"] },
  { id: "other", fields: ["other_adult"] },
  { id: "notes", fields: ["notes"], optional: true },
];
const REQUIRED = [["first_name", (s) => s.first_name.trim()], ["last_name", (s) => s.last_name.trim()], ["phone", (s) => digits(s.phone).length === 10],
  ["street", (s) => s.street.trim()], ["city", (s) => s.city && (s.city !== "other" || s.city_other)], ["zip", (s) => digits(s.zip).length === 5],
  ["mail_street", (s) => s.mail_same !== "no" || s.mail_street.trim()],
  ["adults", (s) => !!s.adults], ["children", (s) => (s.has_children === "yes" && s.children.length) || s.has_children === "no"],
  ["children_ages", (s) => s.no_children || s.children.every((c) => c.age !== "" && c.first_name.trim())],
  ["children_sex", (s) => s.no_children || s.children.every((c) => c.sex === "boy" || c.sex === "girl")]];
// Which form step owns a missing field, for the "check it on screen" landing.
const FIELD_STEP = { first_name: "name", last_name: "name", phone: "phone", street: "street", city: "cityzip", zip: "cityzip", mail_street: "mail_addr", adults: "adults", children: "has_children", children_ages: "child:0:a", children_sex: "child:0:b" };

// Short answers that need no model call. Every word must be a negative word for "no";
// an affirmative must start with a yes-word and carry no digits and no negation.
const NEG_WORDS = new Set(["no", "non", "nope", "nada", "nadie", "ninguno", "ninguna", "ningun", "ningunos", "ningunas", "hay", "tengo", "tenemos", "hijos", "ninos", "nino", "nina", "mas", "none", "nothing", "nobody", "dont", "do", "not", "have", "any", "kids", "children", "gracias", "thanks", "thank", "you", "senor", "senora"]);
function classifyShort(text) {
  const norm = String(text || "").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "").replace(/[^a-z0-9 ]/g, " ").trim();
  const words = norm.split(/\s+/).filter(Boolean);
  const hasDigit = /\d/.test(norm);
  const negated = /^(no|non|nope|not)\b/.test(norm) || /\bno (es|esta|is|son)\b/.test(norm);
  const TRUE_NEG = ["no", "non", "nope", "not", "nada", "nadie", "ninguno", "ninguna", "ningun", "none", "nothing", "nobody", "dont"];
  const isNo = words.length > 0 && !hasDigit && words.every((w) => NEG_WORDS.has(w)) && words.some((w) => TRUE_NEG.includes(w));
  const isSame = !negated && !hasDigit && norm.length < 40 && /^(si|yes|yeah|yep|same|la misma|el mismo|igual|ahi mismo|aqui mismo|esa misma)\b/.test(norm);
  return { isNo, isSame, negated };
}
function leaveVoice() {
  stopSpeaking(); closeOverlay();
  const answered = !!(voice && voice.qi >= 0 && (state.first_name || state.phone || state.street || state.children.length));
  if (voice && voice.helperDefaulted) state.helper = "";
  const miss = nextMissing();
  view = "form"; voice = null;
  const target = miss ? (FIELD_STEP[miss] || "name") : "review";
  cur = screens().includes(target) ? target : "helper";
  errors = (miss && answered) ? validate(cur) : {};
  render();
}

function unlockAudio() {
  if (voiceAudio) return;
  voiceAudio = new Audio();
  voiceAudio.src = "data:audio/mp3;base64,//uQxAAAAAAAAAAAAAAAAAAAAAAAWGluZwAAAA8AAAACAAACcQCAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICA//////////////////////////////////////////////////////////////////8AAABhTEFNRTMuMTAwA8MAAAAAAAAAABQgJAUHQQAB9AAAAnGMHkkIAAAAAAAAAAAAAAAAAAAA//sQxAADgnABGiAAQBCqgCRMAAgEAH///////////////7+n/9FTuQsQH//////2NG0jWUGlio5gLQTOtIoeR2WX////X4s9Ah/////VCZodxgh/////t5r//c2u//sQxAADzgAZ//AQAiEmNuWzwAHKxT6EQP7f///////////////4bsdTcAiSDATSOTRk5c0KpdKfBUyMQ0P//////////////////////////////7cRYnzADw4V//sQxAAD1gAIf/AAIhEN1GerwAA3GbTpvbABg7O2QRK//////////////////mI4Gq0sZLxwwtVMLWmU4cOuN/////////////////////////////+m53BPNWKk7A==";
  voiceAudio.play().catch(() => {});
}
function voicePlay(src, onend) {
  // Whatever happens to the audio (blocked autoplay, missing file, network), the flow
  // must continue: onend runs exactly once, on end, on error, or on a blocked play.
  stopSpeaking();
  if (!voiceAudio) unlockAudio();
  let done = false;
  const finish = () => { if (done) return; done = true; speaking = false; if (onend) onend(); };
  voiceAudio.onended = finish;
  voiceAudio.onerror = finish;
  voiceAudio.src = src; speaking = true;
  voiceAudio.play().catch(finish);
}
async function voiceSay(text, onend) {
  const gen = voiceGen;
  try {
    const r = await fetch(`${config.api_base}/api/tts`, { method: "POST", headers: apiHeaders({ "content-type": "application/json" }), body: JSON.stringify({ lang, text }) });
    if (gen !== voiceGen) return;
    if (r.ok) { voicePlay(URL.createObjectURL(await r.blob()), onend); return; }
  } catch (e) { /* fall through */ }
  if (gen !== voiceGen) return;
  synthSpeak(text, onend);
}
function voiceAsk(key) {
  const name = `${lang}/${key}`;
  if (audioManifest.files[name]) voicePlay(`${base}/static/audio/${name}.mp3`);
  else voiceSay(t(key));
}

function startVoice() {
  unlockAudio();
  view = "voice";
  voice = { qi: -1, phase: "intro", transcript: "", readback: "", error: "", missingKey: null, tries: {} };
  // Voice mode does not ask the helper question; a voice applicant answers for their own family.
  if (!state.helper) { state.helper = "no"; voice.helperDefaulted = true; }
  if (!state.referral_choice) state.referral_choice = "skip";
  if (!state.contact_lang) state.contact_lang = lang;
  render();
  voicePlay(`${base}/static/audio/${lang}/voice_intro.mp3`, () => {});
}
function voiceBegin() {
  if (voice.qi >= 0) return;
  voice.qi = 0; voice.phase = "ask"; render(); voiceAsk("voice_q_" + VOICE_QS[0].id);
}

function currentQuestion() {
  if (voice.missingKey) return { id: "m_" + voice.missingKey, key: "voice_m_" + voice.missingKey, text: t("voice_m_" + voice.missingKey), missing: voice.missingKey, fields: [] };
  const q = VOICE_QS[voice.qi];
  return { ...q, key: "voice_q_" + q.id, text: t("voice_q_" + q.id) };
}

async function voiceTalk() {
  const q = currentQuestion();
  if (rec.state === "recording") { showProcessing(); rec.recorder.stop(); voice.phase = "working"; render(); return; }
  if (rec.state !== "idle") return;
  voice.error = ""; voice.phase = "recording"; render();
  rec.prompt = q.text;
  await toggleRecording(async (text) => {
    if (text === null) { voice.phase = "ask"; render(); return; }  // cancelled
    if (text === false) { voice.error = rec.error === t("speak_busy") ? t("speak_busy") : t("voice_trouble"); voice.phase = "ask"; render(); voiceSay(voice.error); return; }
    if (!text) { voice.error = t("voice_no_sound"); voice.phase = "ask"; render(); voiceSay(t("voice_no_sound")); return; }
    voice.transcript = text;
    try {
      const { isNo, isSame, negated } = classifyShort(text);
      const id = q.id;
      if (id === "mail" && isSame) { state.mail_same = "yes"; state.mail_street = ""; }
      else if (id === "other" && isNo) { state.other_adult = ""; }
      else if (id === "notes" && isNo) { state.notes = ""; }
      else if (id === "notes") { state.notes = text.trim().slice(0, 500); }
      else if ((id === "children" || id === "m_children") && isNo) { state.children = []; state.no_children = true; state.has_children = "no"; }
      else {
        const { fields } = await extractText(text, q.text);
        // A fresh answer replaces what this question owns; other questions' answers stay.
        if (id === "who" || id === "home" || id === "adults") { for (const k of q.fields) if (k in state && !Array.isArray(state[k]) && typeof state[k] !== "boolean") state[k] = ""; }
        if (id === "m_children_ages" || id === "m_children_sex") {
          // Only ages: match by first name, else by position.
          const got = (fields && fields.children) || [];
          const byName = (c) => state.children.find((k) => c.first_name && k.first_name && k.first_name.toLowerCase() === String(c.first_name).toLowerCase());
          got.forEach((c) => {
            const hasAge = c.age !== null && c.age !== undefined && c.age !== "";
            let target = byName(c);
            if (!target) target = state.children.find((k) => (id === "m_children_ages" ? k.age === "" : !k.sex));
            if (!target) return;
            if (hasAge && target.age === "") target.age = String(c.age);
            if (!target.first_name && c.first_name) target.first_name = String(c.first_name).trim();
            if (!target.sex && c.sex) target.sex = c.sex;
          });
        } else if (fields) applyExtracted(fields, { replaceChildren: true });
        if (id === "adults" || id === "m_adults") state.adult_coats = state.adult_coat_sizes.length ? "yes" : "no";
        if (id === "mail") {
          if (negated && !fields?.mail_street) { state.mail_same = "no"; }
          else if (!negated && !fields?.mail_street) { state.mail_same = "yes"; }
        }
        if ((id === "children" || id === "m_children") && !(fields && fields.children && fields.children.length) && negated) { state.children = []; state.no_children = true; state.has_children = "no"; }
        if (id === "other" && fields?.other_adult) state.other_adult = fields.other_adult;
      }
      voice.readback = readbackFor(q); voice.phase = "confirm"; saveDraft(); render();
      voiceSay(voice.readback);
    } catch (e) { console.error(e); voice.error = t("voice_trouble"); voice.phase = "ask"; render(); voiceSay(voice.error); }
  });
  // The microphone may have been refused: say so and offer the screen.
  if (rec.state !== "recording") { voice.phase = "ask"; if (rec.error) { voice.error = t("voice_mic_off"); voiceSay(voice.error); } }
  render();
}

function spaced(p) { const d = digits(p); return d ? d.split("").join(" ").replace(/(\d \d \d) (\d \d \d) /, "$1, $2, ") : ""; }
function readbackFor(q) {
  const s = state; const cityName = s.city === "other" ? s.city_other : s.city;
  const addr = [s.street, s.unit, cityName, s.zip].filter(Boolean).join(", ");
  const child = (c) => {
    const name = c.first_name || t("voice_rb_noname");
    return c.age === "" ? t("voice_rb_child_noage", name, c.sex, c.coat === "yes") : t("voice_rb_child", name, Number(c.age), c.sex, c.coat === "yes");
  };
  switch (q.id) {
    case "who": case "m_first_name": case "m_last_name": case "m_phone": return (s.first_name || s.last_name || s.phone) ? t("voice_rb_who", s.first_name, s.last_name, spaced(s.phone), s.can_text === "yes") : t("voice_rb_nothing");
    case "home": case "m_street": case "m_zip": case "m_city": return addr ? t("voice_rb_home", addr) : t("voice_rb_nothing");
    case "mail": case "m_mail_street": return s.mail_same === "no" ? (s.mail_street ? t("voice_rb_mail", [s.mail_street, s.mail_city, s.mail_zip].filter(Boolean).join(", ")) : t("voice_rb_mail_need")) : t("voice_rb_mail_same");
    case "adults": case "m_adults": return s.adults ? t("voice_rb_adults", Number(s.adults), s.adult_coat_sizes) : t("voice_rb_nothing");
    case "children": case "m_children": case "m_children_ages": case "m_children_sex": return s.no_children ? t("voice_rb_children", []) : (s.children.length ? t("voice_rb_children", s.children.map(child)) : t("voice_rb_nothing"));
    case "other": return t("voice_rb_other", s.other_adult);
    case "notes": return t("voice_rb_notes", s.notes);
  }
  return "";
}

function nextMissing() { const m = REQUIRED.find(([k, ok]) => !ok(state)); return m ? m[0] : null; }

function voiceNext(skipped = false) {
  stopSpeaking();
  voice.transcript = ""; voice.readback = ""; voice.error = "";
  if (voice.missingKey) { voice.missingKey = null; }
  else if (voice.qi < VOICE_QS.length - 1) { voice.qi += 1; voice.phase = "ask"; render(); voiceAsk("voice_q_" + VOICE_QS[voice.qi].id); return; }
  const miss = nextMissing();
  if (miss) {
    voice.tries[miss] = (voice.tries[miss] || 0) + 1;
    if (voice.tries[miss] <= 2) { voice.missingKey = miss; voice.phase = "ask"; render(); voiceAsk("voice_m_" + miss); return; }
    // Asked twice and still missing: hand over to the screen, on the step that owns it.
    voiceSay(t("voice_type_this"), () => leaveVoice());
    return;
  }
  voice.phase = "summary"; voice.qi = VOICE_QS.length; render();
  const summary = [readbackFor({ id: "who" }), readbackFor({ id: "home" }), readbackFor({ id: "mail" }), readbackFor({ id: "adults" }), readbackFor({ id: "children" }), readbackFor({ id: "other" })].join(" ");
  voicePlay(`${base}/static/audio/${lang}/voice_consents.mp3`, () => voiceSay(summary, () => voicePlay(`${base}/static/audio/${lang}/voice_consents_text.mp3`)));
}
function voiceRepeat() { stopSpeaking(); voice.phase = "ask"; voice.transcript = ""; voice.readback = ""; render(); voiceAsk(currentQuestion().key); }

function renderVoice() {
  const total = VOICE_QS.length;
  const dots = `<div class="step-dots" aria-hidden="true">${VOICE_QS.map((_, i) => `<span class="${i <= voice.qi ? "on" : ""}"></span>`).join("")}</div>`;
  const screenBtn = `<p class="small-links"><button type="button" class="btn btn-ghost" data-action="voice-review">✏️ ${t("voice_review")}</button></p>`;
  if (voice.phase === "intro") return `<div class="voice"><p class="q">${t("voice_intro")}</p><button type="button" class="btn btn-primary big" data-action="voice-begin">▶️ ${t("start")}</button>${screenBtn}</div>`;
  if (voice.phase === "summary") {
    return `<div class="voice">${dots}<p class="q">${t("voice_summary_intro")}</p>
      <div class="transcript">${["who", "home", "mail", "adults", "children", "other", "notes"].map((id) => `<p>${esc(readbackFor({ id }))}</p>`).join("")}</div>
      <p class="readback">${t("voice_consents")}</p>
      <button type="button" class="btn btn-primary big" data-action="voice-send" ${sending ? "disabled" : ""}>✅ ${t("voice_send")}</button>
      <button type="button" class="btn btn-ghost big" data-action="voice-review">✏️ ${t("voice_review")}</button>
      ${sendError ? sendErrorCard() : ""}</div>`;
  }
  const q = currentQuestion();
  let body = `<div class="voice">${dots}<p class="q">${esc(q.text)}</p>
    <button type="button" class="btn btn-ghost" data-action="voice-replay" style="margin-bottom:8px" aria-label="${esc(t("read_aloud"))}">🔊</button>`;
  if (voice.phase === "ask" || voice.phase === "recording") {
    body += `<button type="button" class="btn big ${voice.phase === "recording" ? "rec" : "btn-primary"}" data-action="voice-talk">${voice.phase === "recording" ? "⏺ " + t("voice_tap_done") : "🎤 " + t("voice_tap_talk")}</button>`;
    if (voice.error) body += `<p class="error">${esc(voice.error)}</p>`;
    if (q.optional) body += `<p class="small-links"><button type="button" class="btn btn-ghost" data-action="voice-skip">${t("voice_skip")}</button></p>`;
    body += screenBtn;
  } else if (voice.phase === "working") {
    body += `<p class="readback">${t("voice_working")}</p>`;
  } else if (voice.phase === "confirm") {
    body += `<div class="transcript"><strong>${t("voice_heard")}</strong> ${esc(voice.transcript)}</div><p class="readback">${esc(voice.readback)}</p>
      <div class="row2"><button type="button" class="btn btn-primary" data-action="voice-ok">✅ ${t("voice_correct")}</button><button type="button" class="btn btn-ghost" data-action="voice-again">🔁 ${t("voice_again")}</button></div>${screenBtn}`;
  }
  return body + "</div>";
}

// Debug hook for tests: read-only view of internal state.
window.__tcc = { get rec() { return { state: rec.state, error: rec.error }; }, get voice() { return voice; }, get view() { return view; }, get state() { return state; }, get cur() { return cur; }, get screens() { return screens(); }, get audioSrc() { return voiceAudio ? voiceAudio.src : ""; }, get speaking() { return speaking; } };

// ---------- boot ----------
function renderFallback(msg) {
  const phone = (config && config.help_phone) || "+15304792050";
  root.innerHTML = `<div class="closed"><p class="error">${esc(msg)}</p>
    <p class="help-links"><a class="btn btn-help" href="sms:${phone}">💬 ${phone}</a><a class="btn btn-ghost" href="tel:${phone}">📞</a></p>
    <p><button type="button" class="btn btn-primary" data-action="reload">Reload / Recargar</button></p></div>`;
}
const withTimeout = (p, ms) => Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), ms))]);
(async function boot() {
  try { const stored = localStorage.getItem(LANG_KEY); if (stored && stored !== lang && STRINGS[stored]) { lang = stored; strings = STRINGS[lang]; document.documentElement.lang = lang; } } catch (e) {}
  try { config = await withTimeout(fetch(`${base}/config.json`, { cache: "no-store" }).then((r) => r.json()), 15000); }
  catch (e) { renderFallback("We could not load the form. Check your connection, or text us. / No pudimos cargar el formulario. Revise su conexión o mándenos un texto."); return; }
  // A cached page can point at an older script than the one just deployed. If the live
  // config carries a newer build id than this page, reload once with a fresh URL.
  try {
    const mine = root.dataset.v || "";
    const params0 = new URLSearchParams(location.search);
    if (config.asset_v && mine && config.asset_v !== mine && params0.get("b") !== config.asset_v) {
      params0.set("b", config.asset_v);
      location.replace(`${location.pathname}?${params0.toString()}${location.hash}`);
      return;
    }
  } catch (e) {}
  try { const m = await withTimeout(fetch(`${base}/static/audio/manifest.json`, { cache: "no-store" }), 8000); if (m.ok) audioManifest = await m.json(); } catch (e) {}
  gate = computeGate();
  const params = new URLSearchParams(location.search);
  const previewParam = params.has("preview");
  demoMode = params.has("voice");
  previewToken = previewParam ? (params.get("preview") || "") : "";
  let serverOpen = null;
  try {
    const r = await withTimeout(fetch(`${config.api_base}/api/status`, { cache: "no-store", headers: apiHeaders() }), 10000);
    if (r.ok) {
      const st = await r.json();
      config.assist = !!st.assist; config.tts = !!st.tts; config.stt = !!st.stt;
      if (typeof st.open === "boolean") { serverOpen = st.open; gate = st.open ? { open: true, reason: null } : { open: false, reason: st.reason || gate.reason || "closed" }; }
      if (st.season && st.season !== config.season) gate = { open: false, reason: "stale" };
    }
  } catch (e) { /* the client-side gate stands */ }
  // Preview: before opening day a bare ?preview works. Once the season is open, a shared
  // link must not send real families into the test inbox, so preview then needs the token.
  previewMode = previewParam && (serverOpen !== true || !!previewToken);
  if (previewMode && gate.reason !== "stale") gate = { open: true, reason: null };
  try { localStorage.removeItem(REMEMBER_KEY); } catch (e) {}
  loadDraft();
  render();
  const known = (loadCodes()[0] || {}).id;
  if (known && cur === "welcome") { try { sentStatus = await withTimeout(fetchStatus(known), 8000); if (cur === "welcome") { scrollTop = false; render(); } } catch (e) {} }
})();
