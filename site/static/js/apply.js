// Truckee Community Cares application form.
// One screen per step, no framework. State lives in memory and (as a draft) in
// sessionStorage. On submit the answers are encrypted in the browser with age
// to the season recipients in config.json and POSTed as ciphertext. The server
// never sees plaintext. Optional "remember me" keeps a copy in localStorage on
// the applicant's own device for prefill next year; nothing is sent.
import { STRINGS, ADULT_SIZES, CHILD_SIZES } from "./apply-strings.js";
import { Encrypter, armor } from "./age.js";

const STEPS = ["welcome", "you", "home", "household", "children", "programs", "review"];
const DRAFT_KEY = "tcc-draft";
const REMEMBER_KEY = "tcc-remembered";
const LANG_KEY = "tcc-lang";

const root = document.getElementById("app");
const base = root.dataset.base || "";
let lang = root.dataset.lang || "en";
let config = null;
let strings = STRINGS[lang];
let step = 0;
let state = blankState();
let errors = {};
let gate = { open: true, reason: null };
let prefilled = false;
let previewMode = false;   // ?preview: always open, submissions go to the "preview" season

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
    consent_area: false, consent_one: false, consent_true: false, remember: true,
  };
}
function blankChild() { return { first_name: "", age: "", sex: "", school: "", school_other: "", coat: "", coat_size: "" }; }

// ---------- helpers ----------
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const digits = (s) => String(s || "").replace(/\D/g, "");
function t(key, ...args) { const v = strings[key]; return typeof v === "function" ? v(...args) : v ?? key; }
function saveDraft() { try { sessionStorage.setItem(DRAFT_KEY, JSON.stringify({ step, state })); } catch (e) {} }
function loadDraft() { try { const d = JSON.parse(sessionStorage.getItem(DRAFT_KEY)); if (d && d.state) { state = { ...blankState(), ...d.state }; step = d.step || 0; } } catch (e) {} }
function loadRemembered() { try { return JSON.parse(localStorage.getItem(REMEMBER_KEY)); } catch (e) { return null; } }
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
function validate(name) {
  const e = {};
  const req = (k) => { if (!String(state[k] ?? "").trim()) e[k] = t("required"); };
  if (name === "you") {
    if (!state.helper) e.helper = t("pick_one");
    if (state.helper === "yes") { req("helper_name"); if (digits(state.helper_phone).length !== 10) e.helper_phone = t("bad_phone"); }
    req("first_name"); req("last_name");
    if (digits(state.phone).length !== 10) e.phone = t("bad_phone");
    if (!state.can_text) e.can_text = t("pick_one");
    if (state.other_phone && digits(state.other_phone).length !== 10) e.other_phone = t("bad_phone");
  }
  if (name === "home") {
    req("street"); req("city");
    if (state.city === "other") req("city_other");
    if (digits(state.zip).length !== 5) e.zip = t("bad_zip");
    if (!state.mail_same) e.mail_same = t("pick_one");
    if (state.mail_same === "no") { req("mail_street"); req("mail_city"); if (digits(state.mail_zip).length !== 5) e.mail_zip = t("bad_zip"); }
  }
  if (name === "household") {
    if (!state.adults) e.adults = t("pick_one");
    if (!state.adult_coats) e.adult_coats = t("pick_one");
  }
  if (name === "children") {
    if (!state.no_children && state.children.length === 0) e.children = t("required");
    state.children.forEach((c, i) => {
      if (!c.first_name.trim()) e[`child_${i}_first_name`] = t("required");
      const a = Number(c.age); if (c.age === "" || !Number.isInteger(a) || a < 0 || a > 18) e[`child_${i}_age`] = t("bad_age");
      if (!c.sex) e[`child_${i}_sex`] = t("pick_one");
      if (!c.coat) e[`child_${i}_coat`] = t("pick_one");
      if (c.school === "other" && !c.school_other.trim()) e[`child_${i}_school_other`] = t("required");
    });
  }
  if (name === "review") {
    if (!state.consent_area) e.consent_area = t("required");
    if (!state.consent_one) e.consent_one = t("required");
    if (!state.consent_true) e.consent_true = t("required");
  }
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
    <div class="choices ${opts.stack ? "stack" : ""}">${options.map(([v, l]) =>
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
  const name = STEPS[step];
  const nav = (last) => `<div class="nav-row">
    ${step > 0 ? `<button class="btn btn-ghost" data-action="back">${t("back")}</button>` : ""}
    <button class="btn btn-primary" data-action="${last ? "submit" : "next"}">${last ? t("submit") : t("next")}</button></div>`;
  const why = (k) => `<p class="why">${esc(t(k))}</p>`;

  if (name === "welcome") {
    const rem = loadRemembered();
    return `<div class="step"><h1>${t("welcome_title")}</h1>
      ${rem && !prefilled ? `<div class="card"><h2>${esc(t("welcome_back", rem.state.first_name))}</h2><p>${t("welcome_back_text")}</p>
        <button class="btn btn-primary btn-big" data-action="prefill">${t("start")}</button>
        <p style="text-align:center;margin-top:8px"><button class="btn btn-ghost" data-action="fresh">${t("welcome_back_fresh")}</button></p></div>` : ""}
      <p>${t("welcome_intro")}</p><p><strong>${t("welcome_time")}</strong></p>
      <h2>${t("welcome_rules_title")}</h2><ul>${t("welcome_rules").map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
      <div class="card"><h2>${t("phone_path_title")}</h2><p>${t("phone_path_text")}</p>
        <p class="help-links"><a class="btn btn-help" href="sms:${config.help_phone}">💬 ${t("help")}</a><a class="btn btn-help" href="https://wa.me/${config.help_phone.replace(/\D/g, "")}" rel="noopener">🟢 ${t("whatsapp")}</a><a class="btn btn-ghost" href="tel:${config.help_phone}">📞 ${t("call")}</a></p></div>
      ${assistEnabled() ? `<div class="card"><h2>🎤 ${t("freeform_title")}</h2><p>${t("freeform_text")}</p>
        ${sttEnabled() ? `<button type="button" class="btn btn-big ${rec.state === "recording" ? "btn-secondary recording" : "btn-primary"}" data-action="record" ${rec.state === "transcribing" || freeform.busy ? "disabled" : ""}>
          ${rec.state === "recording" ? "⏺ " + t("speak_stop") : rec.state === "transcribing" ? t("speak_transcribing") : "🎤 " + t("speak_start")}</button>
          ${rec.error ? `<p class="error">${esc(rec.error)}</p>` : ""}` : ""}
        <div class="field" style="margin-top:12px"><textarea id="freeform" rows="5" placeholder="${esc(t("freeform_placeholder"))}" ${freeform.busy ? "disabled" : ""}>${esc(freeform.text)}</textarea></div>
        ${freeform.error ? `<p class="error">${t("freeform_error")}</p>` : ""}
        ${freeform.missing ? `<p class="why">${t("freeform_done")}${freeform.missing.length ? ` <strong>${t("freeform_missing")}</strong> ${esc(freeform.missing.join(", "))}` : ""}</p>` : ""}
        <button type="button" class="btn btn-secondary btn-big" data-action="freeform" ${freeform.busy ? "disabled" : ""}>${freeform.busy ? t("freeform_working") : t("freeform_go")}</button></div>` : ""}
      ${rem && !prefilled ? "" : `<button class="btn btn-primary btn-big" data-action="next">${t("start")}</button>`}</div>`;
  }
  if (name === "you") {
    return `<div class="step"><h1>${t("you_title")}</h1>
      ${choice("helper", t("helper_q"), yesno(), { hint: t("helper_hint") })}
      ${state.helper === "yes" ? `<div class="card">${field("helper_name", t("helper_name"))}${field("helper_phone", t("helper_phone"), { type: "tel", inputmode: "tel" })}${field("helper_org", t("helper_org"), { optional: true, hint: t("helper_org_hint") })}</div>` : ""}
      ${why("you_why")}
      ${field("first_name", t("first_name"), { autocomplete: "given-name" })}
      ${field("last_name", t("last_name"), { autocomplete: "family-name" })}
      ${field("phone", t("phone"), { type: "tel", inputmode: "tel", autocomplete: "tel", hint: t("phone_hint") })}
      ${choice("can_text", t("can_text"), yesno())}
      ${field("other_phone", t("other_phone"), { type: "tel", inputmode: "tel", optional: true })}
      ${field("email", t("email"), { type: "email", inputmode: "email", autocomplete: "email", optional: true })}
      ${field("other_adult", t("other_adult"), { optional: true, hint: t("other_adult_hint") })}
      ${choice("contact_lang", t("contact_lang"), [["en", "English"], ["es", "Español"]])}
      ${nav(false)}</div>`;
  }
  if (name === "home") {
    const cities = config.service_area.cities.map((c) => [c, c]).concat([["other", t("city_other")]]);
    return `<div class="step"><h1>${t("home_title")}</h1>${why("home_why")}
      ${field("street", t("street"), { autocomplete: "street-address", hint: t("street_hint") })}
      ${field("unit", t("unit"), { optional: true })}
      ${choice("city", t("city"), cities)}
      ${state.city === "other" ? field("city_other", t("city_other")) : ""}
      ${field("zip", t("zip"), { inputmode: "numeric", autocomplete: "postal-code", maxlength: 5 })}
      ${state.zip && digits(state.zip).length === 5 && !inArea() ? `<p class="why">${esc(t("out_of_area"))}</p>` : ""}
      ${choice("mail_same", t("mail_same"), yesno(), { hint: t("mail_why") })}
      ${state.mail_same === "no" ? field("mail_street", t("mail_street")) + field("mail_city", t("mail_city")) + field("mail_zip", t("mail_zip"), { inputmode: "numeric", maxlength: 5 }) : ""}
      ${nav(false)}</div>`;
  }
  if (name === "household") {
    return `<div class="step"><h1>${t("household_title")}</h1>
      ${choice("adults", t("adults"), [1, 2, 3, 4, 5, 6].map((n) => [n, String(n)]))}
      ${choice("adult_coats", t("adult_coats"), yesno())}
      ${state.adult_coats === "yes" ? `<fieldset class="field"><legend>${t("adult_coat_sizes")}</legend><div class="hint">${t("adult_coat_hint")}</div>
        <div class="choices">${state.adult_coat_sizes.map((s, i) => `<label><input type="checkbox" checked data-action="rm-size" data-i="${i}"> ${esc(s)}</label>`).join("")}</div>
        <div class="choices" style="margin-top:8px">${ADULT_SIZES.map((s) => `<button type="button" class="btn btn-ghost" data-action="add-size" data-size="${s}">+ ${s}</button>`).join("")}</div></fieldset>` : ""}
      ${nav(false)}</div>`;
  }
  if (name === "children") {
    const schools = config.schools.map((s) => [s.id, s[lang] || s.en]);
    const cards = state.children.map((c, i) => `<div class="child-card"><h3>${t("child_n", i + 1)}</h3>
      <button type="button" class="remove" data-action="rm-child" data-i="${i}">${t("remove")}</button>
      ${field(`child_${i}_first_name`, t("child_first"), { value: c.first_name, autocomplete: "off" })}
      ${field(`child_${i}_age`, t("child_age"), { value: c.age, type: "number", inputmode: "numeric", hint: t("child_age_hint") })}
      ${choice(`child_${i}_sex`, t("child_sex"), [["boy", t("boy")], ["girl", t("girl")]], { value: c.sex })}
      ${select(`child_${i}_school`, t("child_school"), schools, { value: c.school, optional: true })}
      ${c.school === "other" ? field(`child_${i}_school_other`, t("child_school_other"), { value: c.school_other }) : ""}
      ${choice(`child_${i}_coat`, t("child_coat"), yesno(), { value: c.coat })}
      ${c.coat === "yes" ? select(`child_${i}_coat_size`, t("child_coat_size"), CHILD_SIZES.map((s) => [s, s]), { value: c.coat_size, optional: true }) : ""}
    </div>`).join("");
    return `<div class="step"><h1>${t("children_title")}</h1>${why("children_why")}
      ${cards}
      ${errors.children ? `<div class="msg" role="alert">${esc(errors.children)}</div>` : ""}
      <button type="button" class="btn btn-secondary btn-big" data-action="add-child">+ ${t("add_child")}</button>
      <div class="field" style="margin-top:14px"><div class="choices"><label><input type="checkbox" name="no_children" ${state.no_children ? "checked" : ""}> ${t("no_children")}</label></div>
        ${state.no_children ? `<div class="hint">${t("no_children_note")}</div>` : ""}</div>
      ${nav(false)}</div>`;
  }
  if (name === "programs") {
    const cb = (k, l, dis) => `<label><input type="checkbox" name="${k}" ${state[k] ? "checked" : ""} ${dis ? "disabled" : ""}> ${esc(l)}</label>`;
    return `<div class="step"><h1>${t("programs_title")}</h1>
      <div class="field"><div class="choices stack">${cb("want_food", t("want_food"))}${cb("want_toys", t("want_toys"), state.children.length === 0)}${cb("want_coats", t("want_coats"))}</div></div>
      ${field("referral", t("referral"), { optional: true, hint: t("referral_hint") })}
      ${field("notes", t("notes"), { type: "textarea", optional: true, hint: t("notes_hint") })}
      ${nav(false)}</div>`;
  }
  if (name === "review") {
    const cityName = state.city === "other" ? state.city_other : state.city;
    const addr = `${state.street}${state.unit ? " " + state.unit : ""}, ${cityName} ${state.zip}`;
    const mail = state.mail_same === "yes" ? addr : `${state.mail_street}, ${state.mail_city} ${state.mail_zip}`;
    const kids = state.children.map((c) => `${c.first_name}, ${c.age}, ${c.sex === "boy" ? t("boy") : t("girl")}${c.coat === "yes" ? ", 🧥" : ""}`).join("<br>") || t("no_children");
    const progs = [state.want_food && t("want_food"), state.want_toys && t("want_toys"), state.want_coats && t("want_coats")].filter(Boolean).join(", ");
    const row = (label, val, s) => `<dt>${esc(label)} <a href="#" class="edit" data-action="goto" data-step="${s}">${t("edit")}</a></dt><dd>${val}</dd>`;
    const cbox = (k, l) => `<label class="${errors[k] ? "error" : ""}"><input type="checkbox" name="${k}" ${state[k] ? "checked" : ""}> ${esc(l)}</label>`;
    return `<div class="step summary"><h1>${t("review_title")}</h1><p>${t("review_text")}</p><dl>
      ${row(t("labels").name, esc(`${state.first_name} ${state.last_name}`), 1)}
      ${row(t("labels").phone, esc(state.phone), 1)}
      ${row(t("labels").address, esc(addr), 2)}
      ${row(t("labels").mailing, esc(mail), 2)}
      ${row(t("labels").adults, esc(`${state.adults}${state.adult_coat_sizes.length ? " · 🧥 " + state.adult_coat_sizes.join(", ") : ""}`), 3)}
      ${row(t("labels").children, kids, 4)}
      ${row(t("labels").programs, esc(progs), 5)}</dl>
      <div class="field"><div class="choices stack">${cbox("consent_area", t("consent_area"))}${cbox("consent_one", t("consent_one"))}${cbox("consent_true", t("consent_true"))}</div>
      ${errors.consent_area || errors.consent_one || errors.consent_true ? `<div class="msg" role="alert">${t("required")}</div>` : ""}</div>
      <div class="field"><div class="choices stack"><label><input type="checkbox" name="remember" ${state.remember ? "checked" : ""}> ${t("remember")}</label></div><div class="hint">${t("remember_hint")}</div></div>
      <p class="why">🔒 ${t("review_privacy")}</p>
      ${nav(true)}</div>`;
  }
}

function render() {
  if (speaking) stopSpeaking();
  const total = STEPS.length - 1; // welcome is step 0, not counted
  const pct = Math.round((step / total) * 100);
  const canSpeak = ("speechSynthesis" in window) || Object.keys(audioManifest.files).length > 0;
  const hb = (icon, label) => `<span class="ico" aria-hidden="true">${icon}</span><span>${label}</span>`;
  const helpBar = `<div class="help-bar">
    ${canSpeak ? `<button type="button" class="btn btn-ghost" data-action="speak" aria-pressed="${speaking}">${speaking ? hb("⏹", t("stop_reading")) : hb("🔊", t("read_aloud"))}</button>` : ""}
    ${assistEnabled() ? `<button type="button" class="btn btn-help" data-action="assist-toggle" aria-expanded="${assist.open}">${hb("❓", t("assist_title"))}</button>` : ""}
    <a class="btn btn-help" href="sms:${config.help_phone}">${hb("💬", t("help"))}</a>
    <a class="btn btn-help" href="https://wa.me/${config.help_phone.replace(/\D/g, "")}" rel="noopener">${hb("🟢", t("whatsapp"))}</a>
    <a class="btn btn-ghost" href="tel:${config.help_phone}">${hb("📞", t("call"))}</a></div>`;
  const top = `<div class="apply-top"><a class="brand" href="${base}/${lang}/" aria-label="Truckee Community Cares"><img src="${base}/static/favicon.svg" alt="" width="36" height="36"><span class="brand-text">Truckee Community Cares</span></a>
    <div class="lang-toggle" role="group" aria-label="Language"><button type="button" data-action="lang" data-lang="en" aria-pressed="${lang === "en"}"><span>English</span></button><button type="button" data-action="lang" data-lang="es" aria-pressed="${lang === "es"}"><span>Español</span></button></div></div>`;
  let body;
  if (view === "done") {
    body = `<div class="done"><h1>✅ ${t("done_title")}</h1><p>${t("done_code")}</p><div class="code">${esc(doneId)}</div>
      <p>${t("done_text")}</p><p class="muted">${t("done_limited")}</p>
      <p><button class="btn btn-ghost" data-action="again">${t("done_again")}</button></p></div>`;
  } else if (!gate.open) {
    body = `<div class="closed"><h1>${gate.reason === "not_open" ? t("not_open_title") : t("closed_title")}</h1>
      <p>${gate.reason === "not_open" ? esc(t("not_open_text", fmtDate(localToUtc(config.opens, config.timezone)))) : t("closed_text")}</p></div>`;
  } else {
    body = (step > 0 ? `<div class="progress"><div class="bar"><div style="width:${pct}%"></div></div><div class="label">${t("step_of", step, total)}</div></div>` : "") + renderStep();
    if (sendError) body += `<div class="card"><p class="error">${t("send_error")}</p><p>${t("send_error_help")}</p><button class="btn btn-primary" data-action="submit">${t("retry")}</button></div>`;
  }
  const panel = assist.open ? `<section class="card assist" aria-label="${esc(t("assist_heading"))}"><h2>${t("assist_heading")} <button type="button" class="btn btn-ghost small" data-action="assist-toggle" style="float:right">${t("assist_close")}</button></h2>
    <p class="muted">${t("assist_intro")}</p>
    <div class="assist-log">${assist.history.map((m, i) => `<p class="${m.role}"><strong>${m.role === "user" ? "🙂" : "🤝"}</strong> ${esc(m.content)}${m.role === "assistant" ? ` <button type="button" class="speak-inline" data-action="speak-text" data-i="${i}" aria-label="${esc(t("read_aloud"))}">🔊</button>` : ""}</p>`).join("")}${assist.busy ? `<p class="muted">${t("assist_thinking")}</p>` : ""}${assist.error ? `<p class="error">${t("assist_error")}</p>` : ""}</div>
    <form data-action="assist-ask" class="assist-form"><input id="assist-q" type="text" placeholder="${esc(t("assist_placeholder"))}" maxlength="500" autocomplete="off" ${assist.busy ? "disabled" : ""}><button class="btn btn-primary" ${assist.busy ? "disabled" : ""}>${t("assist_send")}</button></form></section>` : "";
  const banner = previewMode ? `<div class="announcement" role="status">${lang === "es" ? "MODO DE PRUEBA. Esta solicitud no cuenta. Las solicitudes reales abren el " : "PREVIEW MODE. This application does not count. Real applications open "}${esc(fmtDate(localToUtc(config.opens, config.timezone)))}.</div>` : "";
  root.innerHTML = top + banner + body + panel + helpBar;
  fitLabels();
  window.scrollTo(0, 0);
  const firstErr = root.querySelector(".invalid input, .invalid select, [role=alert]");
  if (firstErr && Object.keys(errors).length) firstErr.focus?.();
}

// Shrink label text so every button in a row fits with a little relief, whatever the
// language. Runs after each render and on resize. Buttons in one row share a size.
function fitLabels() {
  for (const row of root.querySelectorAll(".help-bar, .lang-toggle")) {
    const labels = [...row.querySelectorAll(".btn > span:not(.ico), button > span:not(.ico)")];
    if (!labels.length) continue;
    const relief = 8;
    let scale = 1;
    for (const l of labels) {
      l.style.fontSize = "";
      const btn = l.closest(".btn, button");
      const avail = btn.clientWidth - relief - (parseFloat(getComputedStyle(btn).paddingLeft) + parseFloat(getComputedStyle(btn).paddingRight));
      if (l.scrollWidth > avail && avail > 0) scale = Math.min(scale, avail / l.scrollWidth);
    }
    if (scale < 1) for (const l of labels) l.style.fontSize = `${Math.max(0.6, scale) * parseFloat(getComputedStyle(l).fontSize)}px`;
  }
}
let fitTimer;
window.addEventListener("resize", () => { clearTimeout(fitTimer); fitTimer = setTimeout(fitLabels, 100); });

// ---------- state updates ----------
function readInputs() {
  root.querySelectorAll("input, select, textarea").forEach((el) => {
    const n = el.name; if (!n) return;
    const m = n.match(/^child_(\d+)_(.+)$/);
    if (m) { const c = state.children[+m[1]]; if (!c) return; if (el.type === "radio") { if (el.checked) c[m[2]] = el.value; } else c[m[2]] = el.value; return; }
    if (el.type === "checkbox") state[n] = el.checked;
    else if (el.type === "radio") { if (el.checked) state[n] = el.value; }
    else state[n] = el.value;
  });
  if (state.children.length === 0) state.want_toys = false; else if (state.want_toys === false && state.children.length) state.want_toys = true;
  saveDraft();
}

let view = "form"; let doneId = ""; let sendError = false; let sending = false; let speaking = false;
let assist = { open: false, history: [], busy: false, error: false };
let audioManifest = { files: {} };   // site/static/audio/manifest.json, pre-rendered screens
let player = null;                    // the one <audio> element in use
const ttsEnabled = () => !!(config && config.tts);
let freeform = { text: "", busy: false, error: false, missing: null, summary: "" };
let rec = { state: "idle", recorder: null, chunks: [], stream: null, error: "" };  // idle | recording | transcribing
const sttEnabled = () => !!(config && config.stt) && !!navigator.mediaDevices?.getUserMedia && "MediaRecorder" in window;
const assistEnabled = () => !!(config && config.assist);

// Read the current screen aloud. Pre-rendered audio (a real voice, rendered at build
// time by bin/build-audio.mjs) when it exists; otherwise the device's own speech engine.
function stopSpeaking() {
  if (player) { player.pause(); player = null; }
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
  return STEPS[step];
}
function speakPage() {
  if (speaking) { stopSpeaking(); return; }
  const key = currentScreenKey();
  const name = `${lang}/${key}`;
  const stepEl = root.querySelector(".step, .done, .closed");
  const domText = stepEl ? [...stepEl.querySelectorAll("h1, h2, p, li, label, legend, .hint, dt, dd")].map((n) => n.textContent.trim()).filter(Boolean).join(". ") : "";
  // Only the confirmation code is dynamic: spoken after the recording, letter by letter.
  const tail = view === "done" ? doneId.split("").join(" ") : "";
  if (audioManifest.files[name]) playUrl(`${base}/static/audio/${name}.mp3`, tail ? () => speakTail(tail) : null);
  else synthSpeak(domText);
}
// Dynamic text after a recording: server voice when the API has it, else the device voice.
async function speakTail(text) {
  if (ttsEnabled()) {
    try {
      const r = await fetch(`${config.api_base}/api/tts`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ lang, text }) });
      if (r.ok) { playUrl(URL.createObjectURL(await r.blob())); return; }
    } catch (e) { /* fall through */ }
  }
  synthSpeak(text);
}
// Speak a piece of dynamic text (a help answer) with the server voice, else the device voice.
async function speakText(text) {
  if (speaking) { stopSpeaking(); return; }
  if (ttsEnabled()) {
    try {
      const r = await fetch(`${config.api_base}/api/tts`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ lang, text }) });
      if (r.ok) { playUrl(URL.createObjectURL(await r.blob())); return; }
    } catch (e) { /* fall through */ }
  }
  synthSpeak(text);
}

root.addEventListener("click", async (ev) => {
  const el = ev.target.closest("[data-action]"); if (!el) return;
  const a = el.dataset.action;
  if (a === "assist-ask") return; // the form's submit handler owns this; re-rendering here would drop the question
  if (el.tagName === "A") ev.preventDefault();
  if (a === "speak") { speakPage(); return; }
  if (a === "speak-text") { speakText(assist.history[+el.dataset.i]?.content || ""); return; }
  if (a === "assist-toggle") { readInputs(); assist.open = !assist.open; render(); if (assist.open) root.querySelector("#assist-q")?.focus(); return; }
  if (a === "freeform") { const ta = root.querySelector("#freeform"); freeform.text = ta ? ta.value : ""; await runFreeform(); return; }
  if (a === "record") { await toggleRecording(); return; }
  if (a === "lang") { readInputs(); setLang(el.dataset.lang); return; }
  readInputs();
  if (a === "next") { errors = validate(STEPS[step]); if (Object.keys(errors).length) return render(); step = Math.min(step + 1, STEPS.length - 1); }
  if (a === "back") { errors = {}; step = Math.max(step - 1, 0); }
  if (a === "goto") { errors = {}; step = +el.dataset.step; }
  if (a === "add-child") { state.children.push(blankChild()); state.no_children = false; }
  if (a === "rm-child") { state.children.splice(+el.dataset.i, 1); }
  if (a === "add-size") { state.adult_coat_sizes.push(el.dataset.size); }
  if (a === "rm-size") { state.adult_coat_sizes.splice(+el.dataset.i, 1); }
  if (a === "prefill") { const rem = loadRemembered(); if (rem) { state = { ...blankState(), ...rem.state, consent_area: false, consent_one: false, consent_true: false }; } prefilled = true; step = 1; }
  if (a === "fresh") { try { localStorage.removeItem(REMEMBER_KEY); } catch (e) {} prefilled = true; step = 1; }
  if (a === "again") { state = blankState(); step = 0; view = "form"; doneId = ""; prefilled = true; }
  if (a === "submit") { errors = validate("review"); if (Object.keys(errors).length) return render(); await submit(); return; }
  errors = {}; saveDraft(); render();
});
root.addEventListener("submit", async (ev) => {
  const f = ev.target.closest('[data-action="assist-ask"]'); if (!f) return;
  ev.preventDefault();
  const q = root.querySelector("#assist-q").value.trim(); if (!q || assist.busy) return;
  readInputs();
  assist.history.push({ role: "user", content: q }); assist.busy = true; assist.error = false; render();
  try {
    const r = await fetch(`${config.api_base}/api/help`, { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ lang, question: q, history: assist.history.slice(0, -1).slice(-8) }) });
    if (!r.ok) throw new Error(r.status);
    const { answer } = await r.json();
    assist.history.push({ role: "assistant", content: answer });
  } catch (e) { assist.error = true; }
  assist.busy = false; render(); root.querySelector("#assist-q")?.focus();
});

// Record with the browser's own recorder, transcribe on the server, then fill the form.
function pickMime() {
  for (const m of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"]) if (MediaRecorder.isTypeSupported(m)) return m;
  return "";
}
async function toggleRecording() {
  if (rec.state === "recording") { rec.recorder.stop(); return; }
  rec.error = "";
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
  catch (e) { rec.error = t("mic_denied"); render(); return; }
  const mime = pickMime();
  const recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
  rec = { state: "recording", recorder, chunks: [], stream, error: "" };
  recorder.ondataavailable = (e) => { if (e.data.size) rec.chunks.push(e.data); };
  recorder.onstop = async () => {
    stream.getTracks().forEach((tr) => tr.stop());
    const blob = new Blob(rec.chunks, { type: recorder.mimeType || "audio/webm" });
    rec.state = "transcribing"; render();
    try {
      const ext = /mp4/.test(blob.type) ? "m4a" : /ogg/.test(blob.type) ? "ogg" : "webm";
      const fd = new FormData(); fd.append("file", blob, `speech.${ext}`); fd.append("lang", lang);
      const r = await fetch(`${config.api_base}/api/transcribe`, { method: "POST", body: fd });
      if (!r.ok) throw new Error(r.status);
      const { text } = await r.json();
      if (!text) throw new Error("empty");
      const ta = root.querySelector("#freeform");
      freeform.text = ((ta ? ta.value : freeform.text).trim() + " " + text).trim();
      rec = { state: "idle", recorder: null, chunks: [], stream: null, error: "" };
      await runFreeform();
      return;
    } catch (e) { console.error(e); rec = { state: "idle", recorder: null, chunks: [], stream: null, error: t("speak_error") }; }
    render();
  };
  recorder.start();
  render();
  // Safety stop at 90 seconds so a forgotten recording does not run forever.
  setTimeout(() => { if (rec.recorder === recorder && recorder.state === "recording") recorder.stop(); }, 90000);
}

async function runFreeform() {
  if (freeform.text.trim().length < 10 || freeform.busy) return;
  freeform.busy = true; freeform.error = false; freeform.missing = null; render();
  try {
    const r = await fetch(`${config.api_base}/api/extract`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ lang, text: freeform.text }) });
    if (!r.ok) throw new Error(r.status);
    const { fields: f, missing } = await r.json();
    if (!f) throw new Error("no fields");
    const cities = config.service_area.cities.map((c) => c.toLowerCase());
    Object.assign(state, {
      first_name: f.first_name || state.first_name, last_name: f.last_name || state.last_name, phone: f.phone || state.phone,
      other_adult: f.other_adult || state.other_adult, street: f.street || state.street, unit: f.unit || state.unit, zip: f.zip || state.zip,
      city: f.city ? (cities.includes(f.city.toLowerCase()) ? config.service_area.cities[cities.indexOf(f.city.toLowerCase())] : "other") : state.city,
      city_other: f.city && !cities.includes(f.city.toLowerCase()) ? f.city : state.city_other,
      mail_same: f.mail_street ? "no" : state.mail_same, mail_street: f.mail_street || state.mail_street,
      adults: f.adults ? String(f.adults) : state.adults, adult_coat_sizes: f.adult_coat_sizes?.length ? f.adult_coat_sizes : state.adult_coat_sizes,
      adult_coats: f.adult_coat_sizes?.length ? "yes" : state.adult_coats,
      want_food: f.want_food || state.want_food, want_coats: f.want_coats || state.want_coats,
    });
    if (f.children?.length) state.children = f.children.map((c) => ({ ...blankChild(), first_name: c.first_name || "", age: c.age ?? "", sex: c.sex || "", coat: c.coat ? "yes" : "" }));
    if (state.children.length) state.want_toys = true;
    freeform.missing = missing || []; prefilled = true; step = 1; saveDraft();
  } catch (e) { console.error(e); freeform.error = true; }
  freeform.busy = false; render();
}

root.addEventListener("change", (ev) => {
  // Re-render for inputs that reveal or hide other fields.
  const n = ev.target.name || "";
  if (["helper", "city", "mail_same", "adult_coats", "no_children", "zip"].includes(n) || /^child_\d+_(school|coat)$/.test(n)) { readInputs(); render(); }
});

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
    children: state.children.map((c) => ({ first_name: c.first_name.trim(), age: Number(c.age), sex: c.sex,
      school: c.school === "other" ? c.school_other.trim() : c.school, coat: c.coat === "yes", coat_size: c.coat_size })),
    programs: { food: !!state.want_food, toys: !!state.want_toys, coats: !!state.want_coats },
    referral: state.referral.trim(), notes: state.notes.trim(),
  };
}

async function submit() {
  if (sending) return; sending = true; sendError = false;
  const btn = root.querySelector('[data-action="submit"]'); if (btn) { btn.disabled = true; btn.textContent = t("sending"); }
  try {
    const enc = new Encrypter();
    for (const r of config.recipients) enc.addRecipient(r);
    const ciphertext = armor.encode(await enc.encrypt(JSON.stringify(payload())));
    const res = await fetch(`${config.api_base}/api/apply`, { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ season: previewMode ? "preview" : config.season, lang, ciphertext }) });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    doneId = data.id;
    if (state.remember) { try { const { consent_area, consent_one, consent_true, ...keep } = state; localStorage.setItem(REMEMBER_KEY, JSON.stringify({ saved: new Date().toISOString(), state: keep })); } catch (e) {} }
    try { sessionStorage.removeItem(DRAFT_KEY); } catch (e) {}
    view = "done";
  } catch (e) {
    console.error(e); sendError = true;
  } finally { sending = false; render(); }
}

// ---------- boot ----------
(async function boot() {
  try { const stored = localStorage.getItem(LANG_KEY); if (stored && stored !== lang && STRINGS[stored]) { lang = stored; strings = STRINGS[lang]; document.documentElement.lang = lang; } } catch (e) {}
  config = await (await fetch(`${base}/config.json`, { cache: "no-store" })).json();
  try { const m = await fetch(`${base}/static/audio/manifest.json`, { cache: "no-store" }); if (m.ok) audioManifest = await m.json(); } catch (e) {}
  gate = computeGate();
  const preview = new URLSearchParams(location.search).has("preview");
  previewMode = preview;
  try { const r = await fetch(`${config.api_base}/api/status`, { cache: "no-store" }); if (r.ok) { const s = await r.json(); config.assist = !!s.assist; config.tts = !!s.tts; config.stt = !!s.stt; if (typeof s.open === "boolean" && !preview) gate = s.open ? { open: true } : { open: false, reason: s.reason || gate.reason || "closed" }; } } catch (e) {}
  if (preview) gate = { open: true, reason: null };
  loadDraft();
  if (location.hash.startsWith("#invite=")) {
    // Reserved: invitation links carry prior answers encrypted with a key in the fragment.
    // The fragment never reaches the server. Decoding lands here once the review tool issues links.
  }
  render();
})();
