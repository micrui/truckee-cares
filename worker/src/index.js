// Truckee Community Cares API: an encrypted inbox on Cloudflare Workers + D1.
//
//   GET  /api/status                 -> { season, open, reason, opens, closes, mode }
//   POST /api/apply                  <- { season, lang, ciphertext }  -> { id }
//   GET  /api/admin/submissions      (Bearer ADMIN_TOKEN) [?season=&since=] -> { items: [...] }
//   PATCH /api/admin/submissions/:id (Bearer) <- { status } -> { ok }
//   DELETE /api/admin/season/:season (Bearer, header X-Confirm: <season>) -> { deleted }
//
// No IP addresses, user agents or referrers are stored. Rate limiting uses the
// Cloudflare ratelimit binding, which keeps counts in memory only.
import season from "../../config/season.json" with { type: "json" };
import { handleHelp, handleExtract } from "./assist.js";
import { handleTts } from "./tts.js";
import { handleTranscribe } from "./stt.js";

const MAX_CIPHERTEXT = 64 * 1024;
const ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"; // no 0/O/1/I/L
const STATUSES = new Set(["new", "fetched", "accepted", "declined", "duplicate", "out_of_area"]);

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

export const PREVIEW_SEASON = "preview";

export function makeId(seasonId, rand = crypto.getRandomValues.bind(crypto)) {
  const bytes = rand(new Uint8Array(5));
  let s = "";
  for (const b of bytes) s += ALPHABET[b % ALPHABET.length];
  const tag = seasonId === PREVIEW_SEASON ? "PV" : String(seasonId).slice(-2);
  return `TCC-${tag}-${s}`;
}

function json(data, status = 200, headers = {}) {
  return new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json", "cache-control": "no-store", ...headers } });
}

function corsHeaders(req, env) {
  const origin = req.headers.get("origin") || "";
  const allowed = (env.ALLOWED_ORIGINS || "").split(",").map((s) => s.trim()).filter(Boolean);
  const ok = allowed.includes(origin);
  return {
    "access-control-allow-origin": ok ? origin : allowed[0] || "",
    "access-control-allow-methods": "GET,POST,PATCH,DELETE,OPTIONS",
    "access-control-allow-headers": "content-type,authorization,x-confirm",
    "access-control-max-age": "86400",
    vary: "origin",
  };
}

function isAdmin(req, env) {
  const h = req.headers.get("authorization") || "";
  if (!env.ADMIN_TOKEN || !h.startsWith("Bearer ")) return false;
  const given = h.slice(7);
  if (given.length !== env.ADMIN_TOKEN.length) return false;
  let diff = 0;
  for (let i = 0; i < given.length; i++) diff |= given.charCodeAt(i) ^ env.ADMIN_TOKEN.charCodeAt(i);
  return diff === 0;
}

export async function handle(req, env, now = Date.now()) {
  const url = new URL(req.url);
  const cors = corsHeaders(req, env);
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
  const path = url.pathname.replace(/\/+$/, "");

  if (req.method === "GET" && path === "/api/status") {
    const g = gate(now);
    return json({ season: season.season, open: g.open, reason: g.reason, opens: season.opens, closes: season.closes, timezone: season.timezone, mode: season.mode, assist: !!env.ANTHROPIC_API_KEY, tts: !!env.OPENAI_API_KEY, stt: !!env.OPENAI_API_KEY }, 200, cors);
  }

  if (req.method === "POST" && path === "/api/apply") {
    if (env.RATE) {
      const ip = req.headers.get("cf-connecting-ip") || "unknown";
      const { success } = await env.RATE.limit({ key: ip });
      if (!success) return json({ error: "rate_limited" }, 429, cors);
    }
    let body;
    try { body = await req.json(); } catch { return json({ error: "bad_json" }, 400, cors); }
    const { ciphertext, lang } = body || {};
    if (typeof ciphertext !== "string" || !ciphertext.startsWith("-----BEGIN AGE ENCRYPTED FILE-----") || ciphertext.length > MAX_CIPHERTEXT) {
      return json({ error: "bad_ciphertext" }, 400, cors);
    }
    // The preview season is always open; it exists so the board can test the whole path.
    const isPreview = body.season === PREVIEW_SEASON;
    if (!isPreview && body.season !== season.season) return json({ error: "wrong_season" }, 400, cors);
    if (!isPreview) {
      const g = gate(now);
      if (!g.open) return json({ error: g.reason }, 403, cors);
    }
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
    if (env.RATE) {
      const { success } = await env.RATE.limit({ key: "assist:" + (req.headers.get("cf-connecting-ip") || "unknown") });
      if (!success) return json({ error: "rate_limited" }, 429, cors);
    }
    const r = path === "/api/help" ? await handleHelp(req, env, season) : await handleExtract(req, env, season);
    return json(r.data, r.status, cors);
  }

  if (req.method === "POST" && path === "/api/transcribe") {
    if (!env.OPENAI_API_KEY) return json({ error: "stt_disabled" }, 503, cors);
    if (env.RATE) {
      const { success } = await env.RATE.limit({ key: "stt:" + (req.headers.get("cf-connecting-ip") || "unknown") });
      if (!success) return json({ error: "rate_limited" }, 429, cors);
    }
    const r = await handleTranscribe(req, env);
    const h = new Headers(r.headers); for (const [k, v] of Object.entries(cors)) h.set(k, v);
    return new Response(r.body, { status: r.status, headers: h });
  }

  if (req.method === "POST" && path === "/api/tts") {
    if (!env.OPENAI_API_KEY) return json({ error: "tts_disabled" }, 503, cors);
    if (env.RATE) {
      const { success } = await env.RATE.limit({ key: "tts:" + (req.headers.get("cf-connecting-ip") || "unknown") });
      if (!success) return json({ error: "rate_limited" }, 429, cors);
    }
    const r = await handleTts(req, env);
    const h = new Headers(r.headers); for (const [k, v] of Object.entries(cors)) h.set(k, v);
    return new Response(r.body, { status: r.status, headers: h });
  }

  if (path.startsWith("/api/admin/")) {
    if (!isAdmin(req, env)) return json({ error: "unauthorized" }, 401, cors);

    if (req.method === "GET" && path === "/api/admin/submissions") {
      const s = url.searchParams.get("season") || season.season;
      const since = url.searchParams.get("since") || "";
      const { results } = await env.DB.prepare(
        "SELECT id, season, lang, created_at, ciphertext, status, updated_at FROM submissions WHERE season = ?1 AND created_at > ?2 ORDER BY created_at")
        .bind(s, since).all();
      return json({ season: s, items: results }, 200, cors);
    }

    let m = path.match(/^\/api\/admin\/submissions\/([A-Z0-9-]+)$/);
    if (req.method === "PATCH" && m) {
      let body; try { body = await req.json(); } catch { return json({ error: "bad_json" }, 400, cors); }
      if (!STATUSES.has(body.status)) return json({ error: "bad_status" }, 400, cors);
      const r = await env.DB.prepare("UPDATE submissions SET status = ?1, updated_at = ?2 WHERE id = ?3")
        .bind(body.status, new Date().toISOString(), m[1]).run();
      return json({ ok: true, changed: r.meta?.changes ?? null }, 200, cors);
    }

    m = path.match(/^\/api\/admin\/season\/([A-Za-z0-9-]+)$/);
    if (req.method === "DELETE" && m) {
      if (req.headers.get("x-confirm") !== m[1]) return json({ error: "confirm_header_required" }, 400, cors);
      const r = await env.DB.prepare("DELETE FROM submissions WHERE season = ?1").bind(m[1]).run();
      return json({ deleted: r.meta?.changes ?? null }, 200, cors);
    }
  }

  return json({ error: "not_found" }, 404, cors);
}

export default {
  async fetch(req, env) {
    try { return await handle(req, env); }
    catch (e) { console.error(e); return json({ error: "server_error" }, 500); }
  },
};
