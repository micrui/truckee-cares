# truckee-cares: conventions for AI-assisted maintenance

Read README.md and docs/runbook.md first. This file is the rules.

## What this is

A nonprofit's holiday-assistance intake. Applicants are often immigrant families. The
product promise: **no server ever holds a readable application, and no one is asked
anything that could hurt them.** Every change is judged against that first.

## Rules

1. Plaintext applications exist in exactly two places: the applicant's phone and the
   review tool's SQLite on a board member's Mac. Never add a third. Never log payloads.
   The Worker stores ciphertext, status words, timestamps. Nothing else. No IPs.
2. The form asks only what the program needs. Do not add fields for ID, date of birth,
   income, employer, or status. If a new field is wanted, ask why and what it costs
   the family if leaked.
3. Every string in the form and site exists in English and Mexican Spanish (usted).
   Short sentences. Plain words. Big targets. Test at 375px width. No horizontal scroll.
4. No third-party scripts, fonts, analytics, or CDNs on any page. The age bundle is
   vendored in site/static/js/age.js (rebuild with `npm run vendor`).
5. Season config is config/season.json. Dates, mode (pickup | mail | deliver), the
   announcement banner, the help phone, and the age recipients all live there. The
   Worker bundles it at deploy; the site bakes it at build. Change it, push, deploy.
6. The private key never enters a repo, a chat, a log, or stdout. bin/keygen.mjs writes it
   to ~/.config/truckee-cares/ with mode 600. Do not print it. Do not read it with cat.
7. Claude API use: only in worker/src/assist.js (help chat and free-form fill, with the
   person's own words) and review/judge.py (pairs of applications, minimal fields).
   Model claude-opus-5, effort low. Nothing from these calls is stored server-side.
   Speech: OpenAI gpt-4o-mini-tts, voice nova. Static screens are pre-rendered by
   bin/build-audio.mjs (commit the MP3s; rerun after text changes); only dynamic text
   (help answers) goes through worker/src/tts.js at runtime. Device speech is the fallback.
8. Tests must pass before a push: `npm run test:worker` and `pytest -q tests`.
9. Keep it legible. Server-rendered HTML in the console, vanilla JS in the form, one
   Python module per concern in review/. Prefer a longer plain function over a clever one.

## Where things are

- Form steps and validation: site/static/js/apply.js. Text: site/static/js/apply-strings.js.
- Site pages: site/content/*.yaml (both languages in one file), templates in site/templates.
- API: worker/src/index.js. Live help: worker/src/assist.js. Schema: worker/schema.sql.
- Matching: review/match.py (rules), review/judge.py (Claude), review/console.py (UI).
- Deploy: .github/workflows/pages.yml builds the site; `npm run worker:deploy` ships the API.

## When Mike or a board member states a program fact in chat

Put it in config/season.json or site/content/, not in code. Note the date and who said it
in the commit message.
