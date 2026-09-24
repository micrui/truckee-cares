#!/usr/bin/env node
// Registers the account's workers.dev subdomain (a one-time step wrangler cannot do
// non-interactively). Uses wrangler's own OAuth token from its config file; the token
// never leaves this process and is never printed.
//   node bin/cf-subdomain.mjs <account_id> <subdomain>
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const [account, name] = process.argv.slice(2);
if (!account || !name) { console.error("usage: cf-subdomain.mjs <account_id> <subdomain>"); process.exit(2); }
const candidates = [join(homedir(), "Library", "Preferences", ".wrangler", "config", "default.toml"), join(homedir(), ".wrangler", "config", "default.toml"), join(homedir(), ".config", ".wrangler", "config", "default.toml")];
let token = "";
for (const p of candidates) { try { const m = readFileSync(p, "utf8").match(/oauth_token\s*=\s*"([^"]+)"/); if (m) { token = m[1]; break; } } catch {} }
if (!token) { console.error("no wrangler oauth token found; run: npx wrangler login"); process.exit(1); }
const api = `https://api.cloudflare.com/client/v4/accounts/${account}/workers/subdomain`;
const headers = { authorization: `Bearer ${token}`, "content-type": "application/json" };
const cur = await (await fetch(api, { headers })).json();
if (cur.success && cur.result?.subdomain) { console.log(`already registered: ${cur.result.subdomain}.workers.dev`); process.exit(0); }
const r = await (await fetch(api, { method: "PUT", headers, body: JSON.stringify({ subdomain: name }) })).json();
if (!r.success) { console.error("failed:", JSON.stringify(r.errors)); process.exit(1); }
console.log(`registered: ${r.result.subdomain}.workers.dev`);
