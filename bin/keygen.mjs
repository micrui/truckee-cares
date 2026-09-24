#!/usr/bin/env node
// Generates a season age identity. The secret key is written to a file with
// mode 600 and is never printed. Only the public recipient is printed and
// appended to config/season.json. Back the file up in Apple Passwords.
import { generateIdentity, identityToRecipient } from "age-encryption";
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const label = process.argv[2] || "season";
const dir = join(homedir(), ".config", "truckee-cares");
mkdirSync(dir, { recursive: true, mode: 0o700 });
const path = join(dir, `${label}.key`);
if (existsSync(path)) {
  console.error(`refusing to overwrite ${path}`);
  process.exit(1);
}
const identity = await generateIdentity();
const recipient = await identityToRecipient(identity);
writeFileSync(path, `# truckee-cares ${label} identity, created ${new Date().toISOString()}\n# public key: ${recipient}\n${identity}\n`, { mode: 0o600 });

const cfgPath = new URL("../config/season.json", import.meta.url);
const cfg = JSON.parse(readFileSync(cfgPath, "utf8"));
cfg.recipients = cfg.recipients || [];
if (!cfg.recipients.includes(recipient)) cfg.recipients.push(recipient);
writeFileSync(cfgPath, JSON.stringify(cfg, null, 2) + "\n");
console.log(`identity written to ${path} (mode 600)`);
console.log(`recipient ${recipient} added to config/season.json`);
