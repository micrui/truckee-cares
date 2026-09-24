// Truckee Community Cares API: an encrypted inbox on Cloudflare Workers + D1.
//
//   GET  /api/status                 -> { season, open, reason, opens, closes, mode, assist, tts, stt }
//   POST /api/apply                  <- { season, lang, ciphertext }  -> { id }
//   POST /api/help | /api/extract | /api/transcribe | /api/tts
//                                    live help; only from 45 days before opens to a day after closes,
//                                    or with header x-preview: <PREVIEW_TOKEN>
//   GET  /api/admin/submissions      (Bearer ADMIN_TOKEN) [?season=&since=] -> { items, next_since, has_more }
//   PATCH /api/admin/submissions/:id (Bearer) <- { status } -> { ok }
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
const STATUSES = new Set(["new", "fetched", "accepted", "declined", "duplicate", "out_of_area"]);
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

export async function handle(req, env, now = Date.now()) {
  const url = new URL(req.url);
  const cors = corsHeaders(req, env);
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
    if (!isPreview && !g.open) return json({ error: g.reason }, 403, cors);
    const target = isPreview ? PREVIEW_SEASON : season.season;
    const createdAt = new Date().toISOString();
    for (let attempt = 0; attempt < 5; attempt++) {
      const id = makeId(target);
      try {
        await env.DB.prepare("INSERT INTO submissions (id, season, lang, created_at, ciphertext) VALUES (?1, ?2, ?3, ?4, ?5)")
          .bind(id, target, lang === "es" ? "es" : "en", createdAt, ciphertext).run();
        return json({ id }, 201, cors);
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

  if (path.startsWith("/api/admin/")) {
    if (!isAdmin(req, env)) {
      // Failed guesses count against the caller so the token cannot be brute-forced.
      const hit = await limited(env, "admin:" + clientKey(req), cors);
      return hit || json({ error: "unauthorized" }, 401, cors);
    }

    if (req.method === "GET" && path === "/api/admin/submissions") {
      const s = url.searchParams.get("season") || season.season;
      const since = url.searchParams.get("since") || "";
      const { results } = await env.DB.prepare(
        "SELECT id, season, lang, created_at, ciphertext, status, updated_at FROM submissions WHERE season = ?1 AND created_at > ?2 ORDER BY created_at, id LIMIT 500")
        .bind(s, since).all();
      const nextSince = results.length ? results[results.length - 1].created_at : since;
      return json({ season: s, items: results, next_since: nextSince, has_more: results.length >= PAGE }, 200, cors);
    }

    let m = path.match(/^\/api\/admin\/submissions\/([A-Z0-9-]+)$/);
    if (req.method === "PATCH" && m) {
      let body; try { body = await req.json(); } catch { return json({ error: "bad_json" }, 400, cors); }
      if (!STATUSES.has(body.status)) return json({ error: "bad_status" }, 400, cors);
      const r = await env.DB.prepare("UPDATE submissions SET status = ?1, updated_at = ?2 WHERE id = ?3")
        .bind(body.status, new Date().toISOString(), m[1]).run();
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
    try { return await handle(req, env); }
    catch (e) { console.error(e?.name, e?.status ?? ""); return json({ error: "server_error" }, 500); }
  },
};
