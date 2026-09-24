// Run: npm run test:worker   (node --test, no network, fake D1)
import { test } from "node:test";
import assert from "node:assert/strict";
import { handle, gate, makeId, localToUtc } from "../src/index.js";

function fakeDB() {
  const rows = new Map();
  return {
    rows,
    prepare(sql) {
      let args = [];
      const stmt = {
        bind(...a) { args = a; return stmt; },
        async run() {
          if (sql.startsWith("INSERT")) {
            if (rows.has(args[0])) throw new Error("UNIQUE constraint failed");
            rows.set(args[0], { id: args[0], season: args[1], lang: args[2], created_at: args[3], ciphertext: args[4], status: "new", updated_at: null });
            return { meta: { changes: 1 } };
          }
          if (sql.startsWith("UPDATE")) { const r = rows.get(args[2]); if (r) { r.status = args[0]; r.updated_at = args[1]; } return { meta: { changes: r ? 1 : 0 } }; }
          if (sql.startsWith("DELETE")) { let n = 0; for (const [k, v] of rows) if (v.season === args[0]) { rows.delete(k); n++; } return { meta: { changes: n } }; }
        },
        async all() { return { results: [...rows.values()].filter((r) => r.season === args[0] && r.created_at > args[1]) }; },
      };
      return stmt;
    },
  };
}
const env = () => ({ DB: fakeDB(), ADMIN_TOKEN: "secret-token", ALLOWED_ORIGINS: "https://example.org" });
const req = (method, path, { body, headers = {} } = {}) =>
  new Request("https://api.test" + path, { method, headers: { "content-type": "application/json", ...headers }, body: body ? JSON.stringify(body) : undefined });
const CT = "-----BEGIN AGE ENCRYPTED FILE-----\nabc\n-----END AGE ENCRYPTED FILE-----\n";

test("gate respects opens/closes in Pacific time", () => {
  const cfg = { opens: "2026-10-15T00:00:00", closes: "2026-11-15T23:59:59", timezone: "America/Los_Angeles" };
  assert.equal(gate(Date.parse("2026-10-14T23:00:00-07:00"), cfg).open, false);
  assert.equal(gate(Date.parse("2026-10-15T00:01:00-07:00"), cfg).open, true);
  assert.equal(gate(Date.parse("2026-11-16T00:05:00-08:00"), cfg).open, true); // grace
  assert.equal(gate(Date.parse("2026-11-16T00:20:00-08:00"), cfg).reason, "closed");
  assert.equal(localToUtc("2026-07-01T12:00:00", "America/Los_Angeles"), Date.parse("2026-07-01T12:00:00-07:00"));
});

test("makeId shape", () => {
  const id = makeId("2026", (u) => u.fill(7));
  assert.match(id, /^TCC-26-[A-Z2-9]{5}$/);
});

test("status endpoint", async () => {
  const r = await handle(req("GET", "/api/status"), env());
  assert.equal(r.status, 200);
  const j = await r.json();
  assert.ok("open" in j && j.season);
});

test("apply stores ciphertext only and rejects junk", async () => {
  const e = env();
  const season = (await (await handle(req("GET", "/api/status"), e)).json()).season;
  const bad = await handle(req("POST", "/api/apply", { body: { season, lang: "es", ciphertext: '{"plaintext":true}' } }), e, Date.parse("2026-10-20T12:00:00-07:00"));
  assert.equal(bad.status, 400);
  const inSeason = Date.parse("2026-10-20T12:00:00-07:00");
  const closed = await handle(req("POST", "/api/apply", { body: { season, lang: "es", ciphertext: CT } }), e, Date.parse("2026-01-01T00:00:00Z"));
  assert.equal(closed.status, 403);
  const r = await handle(req("POST", "/api/apply", { body: { season, lang: "es", ciphertext: CT } }), e, inSeason);
  {
    const { id } = await r.json();
    assert.match(id, /^TCC-/);
    const row = e.DB.rows.get(id);
    assert.equal(row.ciphertext, CT);
    assert.equal(row.lang, "es");
    assert.equal(Object.keys(row).includes("ip"), false);
  }
});

test("admin endpoints need the token and support list, patch, purge", async () => {
  const e = env();
  e.DB.rows.set("TCC-26-AAAAA", { id: "TCC-26-AAAAA", season: "2026", lang: "en", created_at: "2026-10-20T00:00:00Z", ciphertext: CT, status: "new", updated_at: null });
  assert.equal((await handle(req("GET", "/api/admin/submissions?season=2026"), e)).status, 401);
  const auth = { authorization: "Bearer secret-token" };
  const list = await (await handle(req("GET", "/api/admin/submissions?season=2026", { headers: auth }), e)).json();
  assert.equal(list.items.length, 1);
  const p = await handle(req("PATCH", "/api/admin/submissions/TCC-26-AAAAA", { headers: auth, body: { status: "accepted" } }), e);
  assert.equal(p.status, 200);
  assert.equal(e.DB.rows.get("TCC-26-AAAAA").status, "accepted");
  const noConfirm = await handle(req("DELETE", "/api/admin/season/2026", { headers: auth }), e);
  assert.equal(noConfirm.status, 400);
  const d = await (await handle(req("DELETE", "/api/admin/season/2026", { headers: { ...auth, "x-confirm": "2026" } }), e)).json();
  assert.equal(d.deleted, 1);
});
