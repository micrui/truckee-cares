// Run: npm run test:worker   (node --test, no network, fake D1 and fake ratelimit bindings)
import { test } from "node:test";
import assert from "node:assert/strict";
import worker, { handle, gate, makeId, localToUtc, clientKey, ipv6Prefix, assistWindow, looksLikeAgeArmor } from "../src/index.js";

function fakeDB() {
  const rows = new Map();
  const byTime = (a, b) => (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
  return {
    rows,
    prepare(sql) {
      let args = [];
      const stmt = {
        bind(...a) { args = a; return stmt; },
        async run() {
          if (sql.startsWith("INSERT")) {
            if (rows.has(args[0])) throw new Error("UNIQUE constraint failed");
            rows.set(args[0], { id: args[0], season: args[1], lang: args[2], created_at: args[3], ciphertext: args[4], status: "new", updated_at: null, supersedes: args[5] ?? null, note: "", self_copy: args[6] ?? null });
            return { meta: { changes: 1 } };
          }
          if (sql.startsWith("UPDATE")) { const r = rows.get(args[2]); if (r) { r.status = args[0]; r.updated_at = args[1]; if (args.length > 3) r.note = args[3]; } return { meta: { changes: r ? 1 : 0 } }; }
          if (sql.startsWith("DELETE") && /WHERE id =/.test(sql)) return { meta: { changes: rows.delete(args[0]) ? 1 : 0 } };
          if (sql.startsWith("DELETE")) { let n = 0; for (const [k, v] of rows) if (v.season === args[0]) { rows.delete(k); n++; } return { meta: { changes: n } }; }
        },
        async all() {
          // The admin list: keyset on (created_at, id), the same predicate as the real query.
          const limit = Number(sql.match(/LIMIT (\d+)/)?.[1] || Infinity);
          const [season, since = "", sinceId = ""] = args;
          const results = [...rows.values()]
            .filter((r) => r.season === season && (r.created_at > since || (r.created_at === since && r.id > sinceId)))
            .sort(byTime).slice(0, limit);
          return { results };
        },
        async first() {
          if (/WHERE supersedes = \?1/.test(sql)) return [...rows.values()].find((r) => r.supersedes === args[0]) ?? null;
          if (/WHERE id = \?1/.test(sql)) return rows.get(args[0]) ?? null;
          throw new Error("fake D1: unexpected first() for " + sql);
        },
      };
      return stmt;
    },
  };
}
// success may be a boolean or a function of the key. Every key seen is recorded.
function fakeRate(success = true) {
  const calls = [];
  return { calls, async limit({ key }) { calls.push(key); return { success: typeof success === "function" ? success(key) : success }; } };
}
const env = (over = {}) => ({ DB: fakeDB(), RATE: fakeRate(), RATE_AI: fakeRate(), ADMIN_TOKEN: "secret-token", PREVIEW_TOKEN: "preview-token", ALLOWED_ORIGINS: "https://example.org", ...over });
const req = (method, path, { body, headers = {}, raw } = {}) =>
  new Request("https://api.test" + path, { method, headers: { "content-type": "application/json", "cf-connecting-ip": "203.0.113.7", ...headers }, body: raw ?? (body ? JSON.stringify(body) : undefined) });

// A real age armor starts with the base64 of the age header. Only the header is needed here.
const armor = (plain) => `-----BEGIN AGE ENCRYPTED FILE-----\n${btoa(plain).match(/.{1,64}/g).join("\n")}\n-----END AGE ENCRYPTED FILE-----\n`;
const CT = armor("age-encryption.org/v1\n-> X25519 bWFkZS11cC1lcGhlbWVyYWwtcHVibGljLWtleS1mb3ItdGVzdHM\nc2VhbGVkLWZpbGUta2V5LWZvci10ZXN0cw\n--- bWFj\n\u0000\u0001\u0002\u0003");

const BEFORE = Date.parse("2026-01-01T00:00:00Z");            // long before: gate closed, AI closed
const PRESEASON = Date.parse("2026-09-15T12:00:00-07:00");   // gate not_open, AI window open
const OPEN = Date.parse("2026-10-20T12:00:00-07:00");        // gate open, AI window open
const JUST_AFTER = Date.parse("2026-11-16T12:00:00-08:00");  // gate closed, AI window still open
const AFTER = Date.parse("2026-11-18T12:00:00-08:00");       // both closed

test("gate respects opens/closes in Pacific time", () => {
  const cfg = { opens: "2026-10-15T00:00:00", closes: "2026-11-15T23:59:59", timezone: "America/Los_Angeles" };
  assert.equal(gate(Date.parse("2026-10-14T23:00:00-07:00"), cfg).open, false);
  assert.equal(gate(Date.parse("2026-10-15T00:01:00-07:00"), cfg).open, true);
  assert.equal(gate(Date.parse("2026-11-16T00:05:00-08:00"), cfg).open, true); // grace
  assert.equal(gate(Date.parse("2026-11-16T00:20:00-08:00"), cfg).reason, "closed");
  assert.equal(localToUtc("2026-07-01T12:00:00", "America/Los_Angeles"), Date.parse("2026-07-01T12:00:00-07:00"));
});

test("assist window is 45 days before opens to a day after closes", () => {
  const cfg = { opens: "2026-10-15T00:00:00", closes: "2026-11-15T23:59:59", timezone: "America/Los_Angeles" };
  assert.equal(assistWindow(Date.parse("2026-08-30T12:00:00-07:00"), cfg), false);
  assert.equal(assistWindow(Date.parse("2026-09-01T12:00:00-07:00"), cfg), true);
  assert.equal(assistWindow(OPEN, cfg), true);
  assert.equal(assistWindow(JUST_AFTER, cfg), true);
  assert.equal(assistWindow(AFTER, cfg), false);
});

test("makeId shape", () => {
  const id = makeId("2026", (u) => u.fill(7));
  assert.match(id, /^TCC-26-[A-Z2-9]{5}$/);
});

test("clientKey: IPv4 as is, IPv6 collapsed to its /64", () => {
  const withIp = (ip) => new Request("https://api.test/", { headers: ip == null ? {} : { "cf-connecting-ip": ip } });
  assert.equal(clientKey(withIp("203.0.113.7")), "203.0.113.7");
  assert.equal(clientKey(withIp("2600:1700::1")), "2600:1700:0000:0000::/64");
  assert.equal(clientKey(withIp("2600:1700:abcd:1234:5678:9abc:def0:1")), "2600:1700:abcd:1234::/64");
  assert.equal(clientKey(withIp("2600:1700:abcd:1234::")), "2600:1700:abcd:1234::/64");
  assert.equal(clientKey(withIp("::1")), "0000:0000:0000:0000::/64");
  assert.equal(clientKey(withIp("::ffff:198.51.100.9")), "198.51.100.9");
  assert.equal(clientKey(withIp(null)), "unknown");
  assert.equal(clientKey(withIp("not-an-ip")), "unknown");
  assert.equal(ipv6Prefix("1::2::3"), null);
  assert.equal(ipv6Prefix("1:2:3:4:5:6:7:8:9"), null);
  // Two interface ids in one /64 share a bucket; a different /64 does not.
  assert.equal(clientKey(withIp("2600:1700:aaaa:bbbb:1:2:3:4")), clientKey(withIp("2600:1700:aaaa:bbbb:ffff:ffff:ffff:ffff")));
  assert.notEqual(clientKey(withIp("2600:1700:aaaa:bbbb::1")), clientKey(withIp("2600:1700:aaaa:bbbc::1")));
});

test("status endpoint reports the AI routes off outside the window unless previewing", async () => {
  const e = env({ ANTHROPIC_API_KEY: "a", OPENAI_API_KEY: "o" });
  const r = await handle(req("GET", "/api/status"), e, OPEN);
  assert.equal(r.status, 200);
  const j = await r.json();
  assert.ok("open" in j && j.season);
  assert.deepEqual([j.assist, j.tts, j.stt], [true, true, true]);
  const closed = await (await handle(req("GET", "/api/status"), e, BEFORE)).json();
  assert.deepEqual([closed.assist, closed.tts, closed.stt], [false, false, false]);
  const previewing = await (await handle(req("GET", "/api/status", { headers: { "x-preview": "preview-token" } }), e, BEFORE)).json();
  assert.deepEqual([previewing.assist, previewing.tts, previewing.stt], [true, true, true]);
  const wrongToken = await (await handle(req("GET", "/api/status", { headers: { "x-preview": "preview-tokeN" } }), e, BEFORE)).json();
  assert.equal(wrongToken.assist, false);
  const noKeys = await (await handle(req("GET", "/api/status"), env(), OPEN)).json();
  assert.deepEqual([noKeys.assist, noKeys.tts, noKeys.stt], [false, false, false]);
});

test("CORS preflight allows the preview header", async () => {
  const r = await handle(req("OPTIONS", "/api/help", { headers: { origin: "https://example.org" } }), env());
  assert.equal(r.status, 204);
  assert.match(r.headers.get("access-control-allow-headers"), /\bx-preview\b/);
  assert.equal(r.headers.get("access-control-allow-origin"), "https://example.org");
});

test("apply stores ciphertext only and rejects junk", async () => {
  const e = env();
  const season = (await (await handle(req("GET", "/api/status"), e)).json()).season;
  const bad = await handle(req("POST", "/api/apply", { body: { season, lang: "es", ciphertext: '{"plaintext":true}' } }), e, OPEN);
  assert.equal(bad.status, 400);
  const closed = await handle(req("POST", "/api/apply", { body: { season, lang: "es", ciphertext: CT } }), e, BEFORE);
  assert.equal(closed.status, 403);
  const r = await handle(req("POST", "/api/apply", { body: { season, lang: "es", ciphertext: CT } }), e, OPEN);
  assert.equal(r.status, 201);
  const { id } = await r.json();
  assert.match(id, /^TCC-/);
  const row = e.DB.rows.get(id);
  assert.equal(row.ciphertext, CT);
  assert.equal(row.lang, "es");
  assert.equal(Object.keys(row).includes("ip"), false);
});

test("apply accepts only armor whose first line decodes to the age header, under 16 KB", async () => {
  const e = env();
  const post = (ciphertext) => handle(req("POST", "/api/apply", { body: { season: "2026", lang: "en", ciphertext } }), e, OPEN);
  assert.equal(looksLikeAgeArmor(CT), true);
  assert.equal((await post(CT)).status, 201);
  const fakes = [
    "-----BEGIN AGE ENCRYPTED FILE-----\nabc\n-----END AGE ENCRYPTED FILE-----\n",   // old test junk
    armor("hello world, this is not an age file at all"),                           // wrong header
    "-----BEGIN AGE ENCRYPTED FILE-----\n" + btoa("age-encryption.org/v1\n") + "\n", // no END line
    "-----BEGIN AGE ENCRYPTED FILE-----\n<script>\n-----END AGE ENCRYPTED FILE-----\n",
    CT.replace(/^([A-Za-z0-9+\/]{10})/m, "$1!!"),                                    // not base64
    CT.replace("BEGIN AGE", "BEGIN PGP"),
    "",
  ];
  for (const f of fakes) {
    assert.equal(looksLikeAgeArmor(f), false, JSON.stringify(f.slice(0, 40)));
    assert.equal((await post(f)).status, 400);
  }
  const big = armor("age-encryption.org/v1\n" + "x".repeat(17 * 1024));
  assert.ok(big.length > 16 * 1024 && big.length < 64 * 1024);
  assert.equal((await post(big)).status, 400);
  const nearLimit = armor("age-encryption.org/v1\n" + "x".repeat(11 * 1024));
  assert.ok(nearLimit.length < 16 * 1024);
  assert.equal((await post(nearLimit)).status, 201);
  assert.equal(e.DB.rows.size, 2);
});

test("apply is rate limited per address and fails closed without the binding", async () => {
  const e = env({ RATE: fakeRate(false) });
  const r = await handle(req("POST", "/api/apply", { body: { season: "2026", lang: "en", ciphertext: CT } }), e, OPEN);
  assert.equal(r.status, 429);
  assert.equal((await r.json()).error, "rate_limited");
  assert.deepEqual(e.RATE.calls, ["203.0.113.7"]);
  assert.equal(e.DB.rows.size, 0);

  const v6 = env();
  const ok = await handle(req("POST", "/api/apply", { body: { season: "2026", lang: "en", ciphertext: CT }, headers: { "cf-connecting-ip": "2600:1700:1234:5678:abcd:ef01:2345:6789" } }), v6, OPEN);
  assert.equal(ok.status, 201);
  assert.deepEqual(v6.RATE.calls, ["2600:1700:1234:5678::/64"]);

  const none = env({ RATE: undefined });
  const r503 = await handle(req("POST", "/api/apply", { body: { season: "2026", lang: "en", ciphertext: CT } }), none, OPEN);
  assert.equal(r503.status, 503);
  assert.equal((await r503.json()).error, "rate_limit_unavailable");
  assert.equal(none.DB.rows.size, 0);
});

test("AI routes: 403 outside the window, preview header opens them, RATE_AI bucket, fail closed", async () => {
  const keys = { ANTHROPIC_API_KEY: "a", OPENAI_API_KEY: "o" };
  const routes = ["/api/help", "/api/extract", "/api/transcribe", "/api/tts"];
  for (const path of routes) {
    // Outside the window: refused before any rate-limit or model call.
    const e = env({ ...keys });
    const closed = await handle(req("POST", path, { body: { lang: "en", question: "hi", text: "hi" } }), e, BEFORE);
    assert.equal(closed.status, 403, path);
    assert.equal((await closed.json()).error, "assist_closed");
    assert.deepEqual(e.RATE_AI.calls, []);
    assert.deepEqual(e.RATE.calls, []);
    // A valid preview header gets past the window check; the tripped limiter proves it.
    const pv = env({ ...keys, RATE_AI: fakeRate(false) });
    const r = await handle(req("POST", path, { body: { lang: "en", question: "hi", text: "hi" }, headers: { "x-preview": "preview-token" } }), pv, BEFORE);
    assert.equal(r.status, 429, path);
    assert.equal(pv.RATE_AI.calls.length, 1);
    assert.match(pv.RATE_AI.calls[0], /^(assist|stt|tts):203\.0\.113\.7$/);
    assert.deepEqual(pv.RATE.calls, [], "AI routes must not spend the apply bucket");
    // A wrong header is the same as none.
    const bad = await handle(req("POST", path, { body: {}, headers: { "x-preview": "wrong" } }), env({ ...keys }), BEFORE);
    assert.equal(bad.status, 403, path);
    // Inside the window: the limiter runs; missing binding fails closed.
    const inWindow = env({ ...keys, RATE_AI: fakeRate(false) });
    assert.equal((await handle(req("POST", path, { body: {} }), inWindow, PRESEASON)).status, 429, path);
    assert.equal((await handle(req("POST", path, { body: {} }), inWindow, JUST_AFTER)).status, 429, path);
    const none = env({ ...keys, RATE_AI: undefined });
    const r503 = await handle(req("POST", path, { body: {} }), none, OPEN);
    assert.equal(r503.status, 503, path);
    assert.equal((await r503.json()).error, "rate_limit_unavailable");
    // No vendor key: disabled, whatever the window.
    assert.equal((await handle(req("POST", path, { body: {} }), env(), OPEN)).status, 503, path);
  }
});

test("admin endpoints need the token and support list, patch, purge", async () => {
  const e = env();
  e.DB.rows.set("TCC-26-AAAAA", { id: "TCC-26-AAAAA", season: "2026", lang: "en", created_at: "2026-10-20T00:00:00Z", ciphertext: CT, status: "new", updated_at: null });
  assert.equal((await handle(req("GET", "/api/admin/submissions?season=2026"), e, AFTER)).status, 401);
  const auth = { authorization: "Bearer secret-token" };
  const list = await (await handle(req("GET", "/api/admin/submissions?season=2026", { headers: auth }), e, AFTER)).json();
  assert.equal(list.items.length, 1);
  assert.equal(list.next_since, "2026-10-20T00:00:00Z");
  assert.equal(list.has_more, false);
  const p = await handle(req("PATCH", "/api/admin/submissions/TCC-26-AAAAA", { headers: auth, body: { status: "accepted" } }), e, AFTER);
  assert.equal(p.status, 200);
  assert.equal(e.DB.rows.get("TCC-26-AAAAA").status, "accepted");
  const noConfirm = await handle(req("DELETE", "/api/admin/season/2026", { headers: auth }), e, AFTER);
  assert.equal(noConfirm.status, 400);
  const d = await (await handle(req("DELETE", "/api/admin/season/2026", { headers: { ...auth, "x-confirm": "2026" } }), e, AFTER)).json();
  assert.equal(d.deleted, 1);
});

test("failed admin auth is rate limited by address", async () => {
  const e = env({ RATE: fakeRate((key) => !key.startsWith("admin:")) });
  const r = await handle(req("GET", "/api/admin/submissions", { headers: { authorization: "Bearer nope" } }), e, AFTER);
  assert.equal(r.status, 429);
  assert.deepEqual(e.RATE.calls, ["admin:203.0.113.7"]);
  const ok = env();
  assert.equal((await handle(req("GET", "/api/admin/submissions"), ok, AFTER)).status, 401);
  assert.deepEqual(ok.RATE.calls, ["admin:203.0.113.7"]);
  // A good token never touches the limiter.
  assert.equal((await handle(req("GET", "/api/admin/submissions", { headers: { authorization: "Bearer secret-token" } }), ok, AFTER)).status, 200);
  assert.equal(ok.RATE.calls.length, 1);
  // Same length, one char off: still rejected (constant-time compare, not a prefix match).
  assert.equal((await handle(req("GET", "/api/admin/submissions", { headers: { authorization: "Bearer secret-tokeN" } }), ok, AFTER)).status, 401);
});

test("admin list is keyset paginated on (created_at, id): ties at the page edge are not lost or repeated", async () => {
  const e = env();
  const auth = { authorization: "Bearer secret-token" };
  // 503 rows; rows 498..502 share one timestamp, so the first page ends inside a tie.
  const stamp = (i) => (i >= 498 ? "2026-10-20T00:00:00Z" : `2026-10-${String(15 + Math.floor(i / 100)).padStart(2, "0")}T00:${String(i % 100).padStart(2, "0")}:00Z`);
  for (let i = 0; i < 503; i++) {
    const id = "TCC-26-" + String(i).padStart(5, "0");
    e.DB.rows.set(id, { id, season: "2026", lang: "en", created_at: stamp(i), ciphertext: CT, status: "new", updated_at: null });
  }
  e.DB.rows.set("TCC-PV-ZZZZZ", { id: "TCC-PV-ZZZZZ", season: "preview", lang: "en", created_at: "2026-10-15T00:00:00Z", ciphertext: CT, status: "new", updated_at: null });
  const list = (qs) => handle(req("GET", "/api/admin/submissions?season=2026" + qs, { headers: auth }), e, AFTER).then((r) => r.json());
  const p1 = await list("");
  assert.equal(p1.items.length, 500);
  assert.equal(p1.has_more, true);
  assert.equal(p1.items[0].id, "TCC-26-00000");
  assert.equal(p1.next_since, "2026-10-20T00:00:00Z");
  assert.equal(p1.next_id, "TCC-26-00499");
  assert.ok(p1.items.every((r) => r.season === "2026"));
  const p2 = await list(`&since=${encodeURIComponent(p1.next_since)}&since_id=${p1.next_id}`);
  assert.deepEqual(p2.items.map((r) => r.id), ["TCC-26-00500", "TCC-26-00501", "TCC-26-00502"]);
  assert.equal(p2.has_more, false);
  assert.equal(p2.next_since, "2026-10-20T00:00:00Z");
  assert.equal(p2.next_id, "TCC-26-00502");
  const p3 = await list(`&since=${encodeURIComponent(p2.next_since)}&since_id=${p2.next_id}`);
  assert.deepEqual(p3.items, []);
  assert.equal(p3.has_more, false);
  assert.equal(p3.next_since, p2.next_since);
  assert.equal(p3.next_id, p2.next_id);
  // Every row seen exactly once across the pages.
  const seen = [...p1.items, ...p2.items].map((r) => r.id);
  assert.equal(new Set(seen).size, 503);
  // An older client that sends only `since` gets the whole tie again (it skips by id), never a gap.
  const noId = await list(`&since=${encodeURIComponent(p1.next_since)}`);
  assert.deepEqual(noId.items.map((r) => r.id), ["TCC-26-00498", "TCC-26-00499", "TCC-26-00500", "TCC-26-00501", "TCC-26-00502"]);
  assert.equal(noId.next_id, "TCC-26-00502");
});

test("admin can fetch one submission by id", async () => {
  const e = env();
  const auth = { authorization: "Bearer secret-token" };
  e.DB.rows.set("TCC-26-ONE01", { id: "TCC-26-ONE01", season: "2026", lang: "es", created_at: "2026-10-20T00:00:00Z", ciphertext: CT, status: "new", updated_at: null });
  assert.equal((await handle(req("GET", "/api/admin/submissions/TCC-26-ONE01"), e, AFTER)).status, 401);
  const r = await handle(req("GET", "/api/admin/submissions/TCC-26-ONE01", { headers: auth }), e, AFTER);
  assert.equal(r.status, 200);
  const { item } = await r.json();
  assert.equal(item.id, "TCC-26-ONE01");
  assert.equal(item.ciphertext, CT);
  assert.equal(item.lang, "es");
  const missing = await handle(req("GET", "/api/admin/submissions/TCC-26-NOPE1", { headers: auth }), e, AFTER);
  assert.equal(missing.status, 404);
  assert.equal((await missing.json()).error, "not_found");
});

test("PATCH with a null or non-object body is a 400, not a crash", async () => {
  const e = env();
  const auth = { authorization: "Bearer secret-token" };
  e.DB.rows.set("TCC-26-AAAAA", { id: "TCC-26-AAAAA", season: "2026", lang: "en", created_at: "2026-10-20T00:00:00Z", ciphertext: CT, status: "new", updated_at: null });
  for (const raw of ["null", "5", '"accepted"', "[]"]) {
    const r = await handle(req("PATCH", "/api/admin/submissions/TCC-26-AAAAA", { headers: auth, raw }), e, AFTER);
    assert.equal(r.status, 400, raw);
    assert.equal((await r.json()).error, "bad_status");
  }
  assert.equal(e.DB.rows.get("TCC-26-AAAAA").status, "new");
});

test("a thrown error becomes a 500 that still carries the CORS headers", async () => {
  const boom = { prepare() { throw new Error("D1 is down"); } };
  const e = env({ DB: boom });
  const quiet = console.error; console.error = () => {};
  try {
    const r = await worker.fetch(req("GET", "/api/admin/submissions?season=2026", { headers: { authorization: "Bearer secret-token", origin: "https://example.org" } }), e);
    assert.equal(r.status, 500);
    assert.equal((await r.json()).error, "server_error");
    assert.equal(r.headers.get("access-control-allow-origin"), "https://example.org");
    assert.equal(r.headers.get("vary"), "origin");
  } finally {
    console.error = quiet;
  }
});

test("admin can delete one submission", async () => {
  const e = env();
  const auth = { authorization: "Bearer secret-token" };
  e.DB.rows.set("TCC-26-BAD01", { id: "TCC-26-BAD01", season: "2026", lang: "en", created_at: "2026-10-20T00:00:00Z", ciphertext: CT, status: "new", updated_at: null });
  e.DB.rows.set("TCC-26-GOOD1", { id: "TCC-26-GOOD1", season: "2026", lang: "en", created_at: "2026-10-20T00:00:01Z", ciphertext: CT, status: "new", updated_at: null });
  assert.equal((await handle(req("DELETE", "/api/admin/submissions/TCC-26-BAD01"), e, OPEN)).status, 401);
  const r = await handle(req("DELETE", "/api/admin/submissions/TCC-26-BAD01", { headers: auth }), e, OPEN);
  assert.equal(r.status, 200);
  assert.equal((await r.json()).deleted, 1);
  assert.equal(e.DB.rows.has("TCC-26-BAD01"), false);
  assert.equal(e.DB.rows.has("TCC-26-GOOD1"), true);
  const again = await handle(req("DELETE", "/api/admin/submissions/TCC-26-BAD01", { headers: auth }), e, OPEN);
  assert.equal((await again.json()).deleted, 0);
  assert.equal((await handle(req("DELETE", "/api/admin/submissions/lower-case", { headers: auth }), e, OPEN)).status, 404);
});

test("purging the live season is refused while it is open; preview purges any time", async () => {
  const e = env();
  const auth = { authorization: "Bearer secret-token", "x-confirm": "2026" };
  e.DB.rows.set("TCC-26-AAAAA", { id: "TCC-26-AAAAA", season: "2026", lang: "en", created_at: "2026-10-20T00:00:00Z", ciphertext: CT, status: "new", updated_at: null });
  e.DB.rows.set("TCC-PV-AAAAA", { id: "TCC-PV-AAAAA", season: "preview", lang: "en", created_at: "2026-10-20T00:00:00Z", ciphertext: CT, status: "new", updated_at: null });
  const refused = await handle(req("DELETE", "/api/admin/season/2026", { headers: auth }), e, OPEN);
  assert.equal(refused.status, 409);
  assert.equal((await refused.json()).error, "season_open");
  assert.equal(e.DB.rows.size, 2);
  const pv = await handle(req("DELETE", "/api/admin/season/preview", { headers: { ...auth, "x-confirm": "preview" } }), e, OPEN);
  assert.equal(pv.status, 200);
  assert.equal((await pv.json()).deleted, 1);
  const later = await handle(req("DELETE", "/api/admin/season/2026", { headers: auth }), e, AFTER);
  assert.equal(later.status, 200);
  assert.equal((await later.json()).deleted, 1);
  assert.equal(e.DB.rows.size, 0);
});

test("preview season bypasses the date gate and is tagged", async () => {
  const e = env();
  const r = await handle(req("POST", "/api/apply", { body: { season: "preview", lang: "en", ciphertext: CT } }), e, BEFORE);
  assert.equal(r.status, 201);
  const { id } = await r.json();
  assert.match(id, /^TCC-PV-/);
  assert.equal(e.DB.rows.get(id).season, "preview");
  const real = await handle(req("POST", "/api/apply", { body: { season: "2026", lang: "en", ciphertext: CT } }), e, BEFORE);
  assert.equal(real.status, 403);
  // After closing, plain preview is still fine for board testing.
  assert.equal((await handle(req("POST", "/api/apply", { body: { season: "preview", lang: "en", ciphertext: CT } }), e, AFTER)).status, 201);
});

test("while the real season is open, preview needs the preview token", async () => {
  const e = env();
  const body = { season: "preview", lang: "en", ciphertext: CT };
  const noHeader = await handle(req("POST", "/api/apply", { body }), e, OPEN);
  assert.equal(noHeader.status, 400);
  assert.equal((await noHeader.json()).error, "wrong_season");
  const wrong = await handle(req("POST", "/api/apply", { body, headers: { "x-preview": "preview-tokeX" } }), e, OPEN);
  assert.equal(wrong.status, 400);
  assert.equal(e.DB.rows.size, 0);
  const ok = await handle(req("POST", "/api/apply", { body, headers: { "x-preview": "preview-token" } }), e, OPEN);
  assert.equal(ok.status, 201);
  assert.match((await ok.json()).id, /^TCC-PV-/);
  // No PREVIEW_TOKEN configured: nothing matches, so preview is closed during the season.
  const unset = env({ PREVIEW_TOKEN: undefined });
  assert.equal((await handle(req("POST", "/api/apply", { body, headers: { "x-preview": "" } }), unset, OPEN)).status, 400);
  // Before opening, plain preview works without the header.
  assert.equal((await handle(req("POST", "/api/apply", { body }), e, PRESEASON)).status, 201);
});

test("an edit supersedes the earlier submission from the same season", async () => {
  const e = env();
  const inSeason = Date.parse("2026-10-20T12:00:00-07:00");
  const first = await (await handle(req("POST", "/api/apply", { body: { season: "2026", lang: "es", ciphertext: CT } }), e, inSeason)).json();
  const edit = await handle(req("POST", "/api/apply", { body: { season: "2026", lang: "es", ciphertext: CT, supersedes: first.id } }), e, inSeason);
  assert.equal(edit.status, 201);
  const { id, supersedes } = await edit.json();
  assert.equal(supersedes, first.id);
  assert.equal(e.DB.rows.get(first.id).status, "superseded");
  assert.equal(e.DB.rows.get(id).supersedes, first.id);
  // a second edit of the already-superseded row, a bad id, or a wrong-season id are refused
  assert.equal((await handle(req("POST", "/api/apply", { body: { season: "2026", lang: "es", ciphertext: CT, supersedes: first.id } }), e, inSeason)).status, 400);
  assert.equal((await handle(req("POST", "/api/apply", { body: { season: "2026", lang: "es", ciphertext: CT, supersedes: "TCC-26-NOPE1" } }), e, inSeason)).status, 400);
  assert.equal((await handle(req("POST", "/api/apply", { body: { season: "preview", lang: "es", ciphertext: CT, supersedes: id } }), e, Date.parse("2026-09-25T12:00:00-07:00"))).status, 400);
});

test("public status lookup by code returns the status word and note only", async () => {
  const e = env();
  const inSeason = Date.parse("2026-10-20T12:00:00-07:00");
  const { id } = await (await handle(req("POST", "/api/apply", { body: { season: "2026", lang: "es", ciphertext: CT } }), e, inSeason)).json();
  const r1 = await (await handle(req("GET", `/api/status/${id}`), e)).json();
  assert.equal(r1.status, "fetched");
  assert.equal(r1.note, "");
  assert.equal("ciphertext" in r1, false);
  const auth = { authorization: "Bearer secret-token" };
  await handle(req("PATCH", `/api/admin/submissions/${id}`, { headers: auth, body: { status: "needs_info", note: "Please confirm your mailing address by text." } }), e);
  const r2 = await (await handle(req("GET", `/api/status/${id}`), e)).json();
  assert.equal(r2.status, "needs_info");
  assert.match(r2.note, /mailing address/);
  assert.equal((await handle(req("GET", "/api/status/TCC-26-ZZZZZ"), e)).status, 404);
  assert.equal((await handle(req("GET", "/api/status/../etc"), e)).status, 404);
  const edit = await (await handle(req("POST", "/api/apply", { body: { season: "2026", lang: "es", ciphertext: CT, supersedes: id } }), e, inSeason)).json();
  const r3 = await (await handle(req("GET", `/api/status/${id}`), e)).json();
  assert.equal(r3.status, "superseded");
  assert.equal(r3.superseded_by, edit.id);
});

test("the applicant's encrypted self copy is stored and returned only on request", async () => {
  const e = env();
  const inSeason = Date.parse("2026-10-20T12:00:00-07:00");
  const { id } = await (await handle(req("POST", "/api/apply", { body: { season: "2026", lang: "es", ciphertext: CT, self_copy: CT } }), e, inSeason)).json();
  const plain = await (await handle(req("GET", `/api/status/${id}`), e)).json();
  assert.equal(plain.has_copy, true);
  assert.equal("self_copy" in plain, false);
  const withCopy = await (await handle(req("GET", `/api/status/${id}?copy=1`), e)).json();
  assert.equal(withCopy.self_copy, CT);
  const bad = await handle(req("POST", "/api/apply", { body: { season: "2026", lang: "es", ciphertext: CT, self_copy: "{\"plain\":true}" } }), e, inSeason);
  assert.equal(bad.status, 400);
});
