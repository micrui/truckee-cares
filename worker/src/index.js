// Truckee Community Cares API: an encrypted inbox on Cloudflare Workers + D1.
//
//   GET  /api/status                 -> { season, open, reason, opens, closes, mode, assist, tts, stt }
//   POST /api/apply                  <- { season, lang, ciphertext, supersedes?, self_copy? }  -> { id, supersedes }
//                                    inside the season window; a replacement of a needs_info
//                                    application is also taken for 30 days after closes
//   GET  /api/status/:id             -> { status, note, superseded_by, ... }  never the application
//   POST /api/help | /api/extract | /api/transcribe | /api/tts
//                                    live help; only from 45 days before opens to a day after closes,
//                                    or with header x-preview: <PREVIEW_TOKEN>
//   GET  /api/admin/submissions      (Bearer ADMIN_TOKEN) [?season=&since=&since_id=]
//                                    -> { items, next_since, next_id, has_more }   keyset pages of 500
//   GET  /api/admin/submissions/:id  (Bearer) -> { item } | 404
//   PATCH /api/admin/submissions/:id (Bearer) <- { status, note? } -> { ok, changed }
//                                    409 superseded: a superseded row keeps that status (note may change)
//                                    400 nothing_supersedes: "superseded" needs a row that names this one
//   DELETE /api/admin/submissions/:id (Bearer) -> { deleted }
//   DELETE /api/admin/season/:season (Bearer, header X-Confirm: <season>) -> { deleted }
//
// No IP addresses, user agents or referrers are stored. Rate limiting uses the
// Cloudflare ratelimit binding, which keeps counts in memory only; the key is the
// IPv4 address or the IPv6 /64, and it never leaves the binding call.
import season from "../../config/season.json" with { type: "json" };
import { handleHelp, handleExtract } from "./assist.js";
import { handleTts } from "./tts.js";
import { handleTranscribe } from "./stt.js";

const MAX_CIPHERTEXT = 16 * 1024;
const PAGE = 500;
const DAY = 24 * 60 * 60 * 1000;
const ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"; // no 0/O/1/I/L
const STATUSES = new Set(["new", "fetched", "needs_info", "accepted", "declined", "duplicate", "out_of_area", "superseded"]);
const MAX_NOTE = 200;   // a short public note from the board, never applicant data
const ID_RE = /^TCC-[A-Z0-9]{2}-[A-Z0-9]{5}$/;
const ARMOR_BEGIN = "-----BEGIN AGE ENCRYPTED FILE-----";
const ARMOR_END = "-----END AGE ENCRYPTED FILE-----";
const AGE_HEADER = "age-encryption.org/v1";

export function localToUtc(iso, tz) {
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

export function gate(now = Date.now(), cfg = season) {
  const opens = localToUtc(cfg.opens, cfg.timezone);
  const closes = localToUtc(cfg.closes, cfg.timezone);
  if (now < opens) return { open: false, reason: "not_open" };
  // Ten minutes of grace so a form started before the deadline still lands.
  if (now > closes + 10 * 60 * 1000) return { open: false, reason: "closed" };
  return { open: true, reason: null };
}

// The paid AI routes run only around the season: 45 days before opens (site testing,
// early questions) to one day after closes. Outside that, PREVIEW_TOKEN opens them.
export function assistWindow(now = Date.now(), cfg = season) {
  const opens = localToUtc(cfg.opens, cfg.timezone);
  const closes = localToUtc(cfg.closes, cfg.timezone);
  return now >= opens - 45 * DAY && now <= closes + DAY;
}

export const PREVIEW_SEASON = "preview";

// After closing, a family answering the board's needs_info request may still replace that
// one application, for LATE_REPLACE_DAYS after closes. Nothing else gets past the gate.
export const LATE_REPLACE_DAYS = 30;
export function lateReplaceOk(prior, target, now = Date.now(), cfg = season) {
  if (!prior || prior.status !== "needs_info" || prior.season !== target) return false;
  const closes = localToUtc(cfg.closes, cfg.timezone);
  return now > closes && now <= closes + LATE_REPLACE_DAYS * DAY;
}

export function makeId(seasonId, rand = crypto.getRandomValues.bind(crypto)) {
  const bytes = rand(new Uint8Array(5));
  let s = "";
  for (const b of bytes) s += ALPHABET[b % ALPHABET.length];
  const tag = seasonId === PREVIEW_SEASON ? "PV" : String(seasonId).slice(-2);
  return `TCC-${tag}-${s}`;
}

// Rate-limit key: the IPv4 address, or the /64 for IPv6 (one prefix per household or
// phone, so a device rotating its interface id does not get a fresh bucket each time).
export function ipv6Prefix(ip) {
  const addr = ip.replace(/^\[|\]$/g, "").split("%")[0].toLowerCase();
  const halves = addr.split("::");
  if (halves.length > 2) return null;
  const head = halves[0] ? halves[0].split(":") : [];
  const tail = halves.length === 2 && halves[1] ? halves[1].split(":") : [];
  const fill = halves.length === 2 ? 8 - head.length - tail.length : 0;
  if (fill < 0) return null;
  const groups = [...head, ...Array(fill).fill("0"), ...tail];
  if (groups.length !== 8 || groups.some((g) => !/^[0-9a-f]{1,4}$/.test(g))) return null;
  return groups.slice(0, 4).map((g) => g.padStart(4, "0")).join(":") + "::/64";
}

export function clientKey(req) {
  const ip = (req.headers.get("cf-connecting-ip") || "").trim();
  if (!ip) return "unknown";
  if (!ip.includes(":")) return /^\d{1,3}(\.\d{1,3}){3}$/.test(ip) ? ip : "unknown";
  const mapped = ip.match(/:(\d{1,3}(?:\.\d{1,3}){3})$/); // ::ffff:1.2.3.4
  if (mapped) return mapped[1];
  return ipv6Prefix(ip) || "unknown";
}

function json(data, status = 200, headers = {}) {
  return new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json", "cache-control": "no-store", ...headers } });
}

// Returns a 429 (or a 503 when the binding is missing: fail closed, never open) or null.
async function limited(env, key, cors, binding = "RATE") {
  const bucket = env[binding];
  if (!bucket) return json({ error: "rate_limit_unavailable" }, 503, cors);
  const { success } = await bucket.limit({ key });
  if (!success) return json({ error: "rate_limited" }, 429, cors);
  return null;
}

function corsHeaders(req, env) {
  const origin = req.headers.get("origin") || "";
  const allowed = (env.ALLOWED_ORIGINS || "").split(",").map((s) => s.trim()).filter(Boolean);
  const ok = allowed.includes(origin);
  return {
    "access-control-allow-origin": ok ? origin : allowed[0] || "",
    "access-control-allow-methods": "GET,POST,PATCH,DELETE,OPTIONS",
    "access-control-allow-headers": "content-type,authorization,x-confirm,x-preview",
    "access-control-max-age": "86400",
    vary: "origin",
  };
}

function tokenMatches(given, expected) {
  if (!expected || typeof given !== "string" || given.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < given.length; i++) diff |= given.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}

function isAdmin(req, env) {
  const h = req.headers.get("authorization") || "";
  return h.startsWith("Bearer ") && tokenMatches(h.slice(7), env.ADMIN_TOKEN);
}

function isPreviewer(req, env) {
  return tokenMatches(req.headers.get("x-preview") || "", env.PREVIEW_TOKEN);
}

// An age armored file: BEGIN line, base64 lines, END line. The first base64 line
// decodes to the age header, so anything else is not something the review tool can open.
export function looksLikeAgeArmor(ciphertext) {
  if (typeof ciphertext !== "string" || ciphertext.length > MAX_CIPHERTEXT) return false;
  const lines = ciphertext.trim().split(/\r?\n/);
  if (lines.length < 3 || lines[0] !== ARMOR_BEGIN || lines[lines.length - 1] !== ARMOR_END) return false;
  const first = lines[1];
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(first)) return false;
  try { return atob(first).startsWith(AGE_HEADER); } catch { return false; }
}

// `cors` is computed by the caller (fetch below) so the headers exist even when handle throws.
export async function handle(req, env, now = Date.now(), cors = corsHeaders(req, env)) {
  const url = new URL(req.url);
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
  const path = url.pathname.replace(/\/+$/, "");
  const assistOk = assistWindow(now) || isPreviewer(req, env);

  if (req.method === "GET" && path === "/api/status") {
    const g = gate(now);
    return json({ season: season.season, open: g.open, reason: g.reason, opens: season.opens, closes: season.closes, timezone: season.timezone, mode: season.mode,
      assist: assistOk && !!env.ANTHROPIC_API_KEY, tts: assistOk && !!env.OPENAI_API_KEY, stt: assistOk && !!env.OPENAI_API_KEY }, 200, cors);
  }

  if (req.method === "POST" && path === "/api/apply") {
    const hit = await limited(env, clientKey(req), cors);
    if (hit) return hit;
    let body;
    try { body = await req.json(); } catch { return json({ error: "bad_json" }, 400, cors); }
    const { ciphertext, lang } = body || {};
    if (!looksLikeAgeArmor(ciphertext)) return json({ error: "bad_ciphertext" }, 400, cors);
    // The preview season exists so the board can test the whole path. While the real
    // season is open it needs the preview token, so nobody can post around the gate.
    const isPreview = body.season === PREVIEW_SEASON;
    if (!isPreview && body.season !== season.season) return json({ error: "wrong_season" }, 400, cors);
    const g = gate(now);
    if (isPreview && g.open && !isPreviewer(req, env)) return json({ error: "wrong_season" }, 400, cors);
    const target = isPreview ? PREVIEW_SEASON : season.season;
    // An edit: the new row replaces an earlier one from the same season. Knowing the
    // earlier code is the proof; it is shown only to the person who submitted it.
    // The earlier row is read before the gate because it can open the gate (below).
    const isEdit = body.supersedes !== undefined && body.supersedes !== null && body.supersedes !== "";
    let prior = null;
    if (isEdit) {
      if (typeof body.supersedes !== "string" || !ID_RE.test(body.supersedes)) return json({ error: "bad_supersedes" }, 400, cors);
      prior = await env.DB.prepare("SELECT id, season, status FROM submissions WHERE id = ?1").bind(body.supersedes).first();
    }
    // The season gate. Inside the window (plus ten minutes of grace) any row may be
    // replaced. After that, only a needs_info row, for 30 days, so a family can answer
    // the board. Everything else, including a fresh application, waits for next year.
    if (!isPreview && !g.open && !lateReplaceOk(prior, target, now)) return json({ error: g.reason }, 403, cors);
    let supersedes = null;
    if (isEdit) {
      if (!prior || prior.season !== target || prior.status === "superseded") return json({ error: "bad_supersedes" }, 400, cors);
      supersedes = prior.id;
    }
    // Optional: the applicant's own copy of their answers, encrypted on the phone to a key
    // that stays on the phone. Lets the same phone edit later. Opaque to us, like the rest.
    let selfCopy = null;
    if (body.self_copy !== undefined && body.self_copy !== null && body.self_copy !== "") {
      if (!looksLikeAgeArmor(body.self_copy)) return json({ error: "bad_self_copy" }, 400, cors);
      selfCopy = body.self_copy;
    }
    const createdAt = new Date().toISOString();
    for (let attempt = 0; attempt < 5; attempt++) {
      const id = makeId(target);
      // The new row and the old row's status change go in one batch, which D1 runs as
      // one transaction: both land or neither does, so a failure between them can never
      // leave two live rows for one family. An id collision throws before anything is
      // committed and the loop tries another id.
      const stmts = [
        env.DB.prepare("INSERT INTO submissions (id, season, lang, created_at, ciphertext, supersedes, self_copy) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)")
          .bind(id, target, lang === "es" ? "es" : "en", createdAt, ciphertext, supersedes, selfCopy),
      ];
      if (supersedes) stmts.push(env.DB.prepare("UPDATE submissions SET status = ?1, updated_at = ?2 WHERE id = ?3").bind("superseded", createdAt, supersedes));
      try {
        await env.DB.batch(stmts);
        return json({ id, supersedes }, 201, cors);
      } catch (e) {
        if (!/UNIQUE|constraint/i.test(String(e))) throw e;
      }
    }
    return json({ error: "id_collision" }, 500, cors);
  }

  if (req.method === "POST" && (path === "/api/help" || path === "/api/extract")) {
    if (!env.ANTHROPIC_API_KEY) return json({ error: "assist_disabled" }, 503, cors);
    if (!assistOk) return json({ error: "assist_closed" }, 403, cors);
    const hit = await limited(env, "assist:" + clientKey(req), cors, "RATE_AI");
    if (hit) return hit;
    const r = path === "/api/help" ? await handleHelp(req, env, season) : await handleExtract(req, env, season);
    return json(r.data, r.status, cors);
  }

  if (req.method === "POST" && path === "/api/transcribe") {
    if (!env.OPENAI_API_KEY) return json({ error: "stt_disabled" }, 503, cors);
    if (!assistOk) return json({ error: "assist_closed" }, 403, cors);
    const hit = await limited(env, "stt:" + clientKey(req), cors, "RATE_AI");
    if (hit) return hit;
    const r = await handleTranscribe(req, env);
    const h = new Headers(r.headers); for (const [k, v] of Object.entries(cors)) h.set(k, v);
    return new Response(r.body, { status: r.status, headers: h });
  }

  if (req.method === "POST" && path === "/api/tts") {
    if (!env.OPENAI_API_KEY) return json({ error: "tts_disabled" }, 503, cors);
    if (!assistOk) return json({ error: "assist_closed" }, 403, cors);
    const hit = await limited(env, "tts:" + clientKey(req), cors, "RATE_AI");
    if (hit) return hit;
    const r = await handleTts(req, env);
    const h = new Headers(r.headers); for (const [k, v] of Object.entries(cors)) h.set(k, v);
    return new Response(r.body, { status: r.status, headers: h });
  }

  // Applicant-facing status: the confirmation code is the key. Only the status word,
  // the board's short public note, and dates come back; never the application.
  let sm = path.match(/^\/api\/status\/([A-Z0-9-]+)$/);
  if (req.method === "GET" && sm) {
    // Status checks use the application tier (RATE, 30 a minute) under their own key, so
    // a family refreshing its card neither spends the apply bucket nor the small AI one.
    const hit = await limited(env, "status:" + clientKey(req), cors, "RATE");
    if (hit) return hit;
    if (!ID_RE.test(sm[1])) return json({ error: "not_found" }, 404, cors);
    const row = await env.DB.prepare("SELECT id, season, status, note, created_at, updated_at, supersedes, self_copy FROM submissions WHERE id = ?1").bind(sm[1]).first();
    if (!row) return json({ error: "not_found" }, 404, cors);
    let superseded_by = null;
    if (row.status === "superseded") {
      const nxt = await env.DB.prepare("SELECT id FROM submissions WHERE supersedes = ?1 ORDER BY created_at DESC LIMIT 1").bind(row.id).first();
      superseded_by = nxt ? nxt.id : null;
    }
    const status = row.status === "new" ? "fetched" : row.status;   // "new" and "fetched" both mean received
    const out = { id: row.id, season: row.season, status, note: row.note || "", created_at: row.created_at, updated_at: row.updated_at, superseded_by, mode: season.mode, has_copy: !!row.self_copy };
    if (url.searchParams.get("copy") === "1" && row.self_copy) out.self_copy = row.self_copy;
    return json(out, 200, cors);
  }

  if (path.startsWith("/api/admin/")) {
    if (!isAdmin(req, env)) {
      // Failed guesses count against the caller so the token cannot be brute-forced.
      const hit = await limited(env, "admin:" + clientKey(req), cors);
      return hit || json({ error: "unauthorized" }, 401, cors);
    }

    if (req.method === "GET" && path === "/api/admin/submissions") {
      // Keyset pagination on (created_at, id). A client without since_id gets the rows at
      // the cursor's timestamp again; it skips the ones it already holds by id.
      const s = url.searchParams.get("season") || season.season;
      const since = url.searchParams.get("since") || "";
      const sinceId = url.searchParams.get("since_id") || "";
      // Never self_copy: the review tool has no key for it and no use for it.
      const { results } = await env.DB.prepare(
        "SELECT id, season, lang, created_at, ciphertext, status, updated_at, supersedes, note FROM submissions WHERE season = ?1 AND (created_at > ?2 OR (created_at = ?2 AND id > ?3)) ORDER BY created_at, id LIMIT 500")
        .bind(s, since, sinceId).all();
      const last = results.length ? results[results.length - 1] : null;
      return json({ season: s, items: results, next_since: last ? last.created_at : since, next_id: last ? last.id : sinceId, has_more: results.length >= PAGE }, 200, cors);
    }

    let m = path.match(/^\/api\/admin\/submissions\/([A-Z0-9-]+)$/);
    if (req.method === "GET" && m) {
      // One row by id, for the review tool's retry of rows it could not read.
      const row = await env.DB.prepare("SELECT id, season, lang, created_at, ciphertext, status, updated_at, supersedes, note FROM submissions WHERE id = ?1").bind(m[1]).first();
      return row ? json({ item: row }, 200, cors) : json({ error: "not_found" }, 404, cors);
    }
    if (req.method === "PATCH" && m) {
      let body; try { body = await req.json(); } catch { return json({ error: "bad_json" }, 400, cors); }
      body = body && typeof body === "object" ? body : {};
      if (!STATUSES.has(body.status)) return json({ error: "bad_status" }, 400, cors);
      const note = typeof body.note === "string" ? body.note.trim().slice(0, MAX_NOTE) : "";
      const row = await env.DB.prepare("SELECT id, status FROM submissions WHERE id = ?1").bind(m[1]).first();
      if (!row) return json({ ok: true, changed: 0 }, 200, cors);
      // "superseded" is what an edit does to the old row; by hand it needs a row that
      // names this one, or the family's only live application would vanish.
      if (body.status === "superseded") {
        const nxt = await env.DB.prepare("SELECT id FROM submissions WHERE supersedes = ?1 ORDER BY created_at DESC LIMIT 1").bind(row.id).first();
        if (!nxt) return json({ error: "nothing_supersedes" }, 400, cors);
      }
      // A superseded row stays superseded. A Mac that pushes before it has seen the
      // replacement must not bring the old row back to life; the note may still change.
      if (row.status === "superseded" && body.status !== "superseded") {
        const nxt = await env.DB.prepare("SELECT id FROM submissions WHERE supersedes = ?1 ORDER BY created_at DESC LIMIT 1").bind(row.id).first();
        return json({ error: "superseded", superseded_by: nxt ? nxt.id : null }, 409, cors);
      }
      const r = await env.DB.prepare("UPDATE submissions SET status = ?1, updated_at = ?2, note = ?4 WHERE id = ?3")
        .bind(body.status, new Date().toISOString(), m[1], note).run();
      return json({ ok: true, changed: r.meta?.changes ?? null }, 200, cors);
    }
    if (req.method === "DELETE" && m) {
      const r = await env.DB.prepare("DELETE FROM submissions WHERE id = ?1").bind(m[1]).run();
      return json({ deleted: r.meta?.changes ?? null }, 200, cors);
    }

    m = path.match(/^\/api\/admin\/season\/([A-Za-z0-9-]+)$/);
    if (req.method === "DELETE" && m) {
      if (req.headers.get("x-confirm") !== m[1]) return json({ error: "confirm_header_required" }, 400, cors);
      // The live season cannot be purged while families are still sending applications.
      if (m[1] === season.season && gate(now).open) return json({ error: "season_open" }, 409, cors);
      const r = await env.DB.prepare("DELETE FROM submissions WHERE season = ?1").bind(m[1]).run();
      return json({ deleted: r.meta?.changes ?? null }, 200, cors);
    }
  }

  return json({ error: "not_found" }, 404, cors);
}

export default {
  async fetch(req, env) {
    // CORS first, outside the try, so a 500 still carries the headers and the form can read it.
    const cors = corsHeaders(req, env);
    try { return await handle(req, env, Date.now(), cors); }
    catch (e) { console.error(e?.name, e?.status ?? ""); return json({ error: "server_error" }, 500, cors); }
  },
};
