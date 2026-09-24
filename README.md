# Truckee Community Cares

Website, application form, and review tools for [Truckee Community Cares](https://www.truckeecommunitycares.com), an all-volunteer nonprofit that gives grocery gift cards, toys and coats to families in Truckee and Soda Springs each December.

Built to be a model for small agencies serving immigrant and underserved communities: **no server ever holds a readable copy of a stored application**, the form works on a phone in English or Spanish for people who read little, and every piece is small enough for one volunteer to maintain. The optional microphone, help chat, and "in your own words" box do send what a person says or types through the Worker to OpenAI (speech) and Anthropic (text) to fill the form or answer a question; nothing from those calls is stored, and the finished application is still encrypted on the phone before it is sent.

## How it fits together

```
site/        static site, English and Spanish, built by Python + Jinja2 → GitHub Pages
  apply      the form: encrypts in the browser (age) to the season's public keys
worker/      Cloudflare Worker + D1: stores ciphertext only; date gate; live help via Claude
review/      local tool on a board member's Mac: decrypts, family database, matching,
             Claude judge for ambiguous pairs, task queue, console, exports
config/season.json   the one file to edit each season (dates, mode, phone, recipients)
```

An application travels: phone → encrypted → Worker (ciphertext) → review tool (decrypts with the keys that only live on the board's Macs) → decisions → status back to the Worker (a word, never a reason) → purge after distribution.

## Quick start

```bash
npm install                # age bundle, wrangler, worker tests
bin/build --base /truckee-cares && bin/dev   # site on http://localhost:8788/
npm run test:worker && .venv/bin/python -m pytest -q tests
```

Season setup, key handling, opening and closing applications, the ICE fallback, and end-of-season purge are in [docs/runbook.md](docs/runbook.md). Design choices and their reasons are in [docs/decisions.md](docs/decisions.md). Conventions for AI-assisted maintenance are in [CLAUDE.md](CLAUDE.md).

## Review tool

```bash
bin/review import ~/Downloads/TCC_2025.xlsx --season 2025   # prior seasons from JotForm
bin/review pull            # fetch and decrypt new applications
bin/review match           # link to families, judge ambiguous pairs, queue tasks
bin/review console         # http://127.0.0.1:8789/  tasks, applications, families, exports
bin/review push            # send accepted/declined/duplicate back to the server
```

Data lives in `~/Library/Application Support/truckee-cares/review.sqlite`, never in this repo.
