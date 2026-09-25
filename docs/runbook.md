# Runbook

Steps for the people who run the season. Run commands from the repo folder
(`cd ~/src/truckee-cares`). No step here prints a key or a token to the screen. If one
does, stop and ask.

## One-time setup

1. **Keys. Two are mandatory before opening day: the season key and a backup key.**

   - Season key: `node bin/keygen.mjs season-2026` writes the private key to
     `~/.config/truckee-cares/season-2026.key` (mode 600) and adds its public recipient to
     `config/season.json`.
   - Backup key: on the same Mac, `node bin/keygen.mjs backup-2026`. Open
     `~/.config/truckee-cares/backup-2026.key` in TextEdit, copy the line that starts with
     `AGE-SECRET-KEY-1` into a new item in Apple Passwords (call it "TCC backup-2026"),
     then delete the file and empty the trash. The backup key now exists only in Apple
     Passwords, which syncs to Mike's other devices.
   - `config/season.json` must now list two or more recipients. The form encrypts every
     application to every recipient in that list, and the list is baked into the site at
     build time. So the change takes effect only after a push to `main` and a green
     "Build and deploy site" run. Confirm at `<site>/config.json`.
   - **Restore rehearsal.** Prove the backup works before anyone applies. On a Mac that
     does not hold `season-2026.key` (Lynette's, or move that file out of
     `~/.config/truckee-cares/` for the test): `bin/set-secret backup-2026.key` and paste
     the `AGE-SECRET-KEY-1` line from Apple Passwords into the dialog. Send one test
     application from the form with `?preview` on the URL, then
     `bin/review pull --season preview`. It should report one new application, and the
     application should open in `bin/review console`. If preview rows were pulled on this
     Mac before, add `--reset`. Afterwards delete `backup-2026.key` from that Mac and put
     the season key back.

     Rehearsed on: ____________ by ____________ (fill this in each season; it is on the
     pre-open checklist below)

2. **Worker.** `npx wrangler login`, then:
   ```
   npx wrangler d1 create truckee-cares            # paste database_id into worker/wrangler.toml
   npx wrangler d1 execute truckee-cares --remote --file worker/schema.sql --config worker/wrangler.toml
   bin/new-admin-token                              # writes ~/.config/truckee-cares/admin-token
   bin/push-secret admin-token ADMIN_TOKEN
   bin/set-secret preview-token                     # any long random word
   bin/push-secret preview-token PREVIEW_TOKEN
   npm run worker:deploy
   ```
   Put the Worker URL in `config/season.json` as `api_base`.

   About the preview token: before the season opens, plain `?preview` on the form URL is
   enough to test. While the season is open, a preview submission needs
   `?preview=<the token you set>` in the URL, so strangers cannot file test applications
   during the real season. Keep the token in Apple Passwords with the others.

3. **Voice and help.** `bin/set-openai-key` opens a dialog and stores the OpenAI key in
   `~/.config/truckee-cares/openai-key`. Then `npm run audio` renders one MP3 per form
   screen per language into `site/static/audio/` (about 5,600 characters, a few cents).
   Commit the MP3s. Re-run after changing any form text or the season's dates; only
   changed screens re-render. For spoken help answers and voice-mode readbacks at runtime,
   also `bin/push-secret openai-key OPENAI_API_KEY`.
   Claude (help chat in the form, and the matching judge in the review tool) uses one key:
   `bin/set-secret anthropic-key`, then `bin/push-secret anthropic-key ANTHROPIC_API_KEY`.

   Never turn on Workers Logs, Logpush, or Tail Workers for this Worker, and never run
   `wrangler tail` while the season is open. What people say to the microphone and the
   help chat passes through the Worker; logs would keep it.

4. **Site.** Push to `main`. GitHub Pages builds from the Actions workflow. Custom domain:
   write it into `config/cname`, add the DNS record, and push.

## Testing before the season opens

Open the form with `?preview` on the end (`?preview=<token>` once the season is open). It
is always open, shows a "preview mode" banner, and stores submissions under the `preview`
season with codes like `TCC-PV-…`. They go through the real path: encrypted, stored, pulled
and matched in the review tool (`bin/review pull --season preview`, then match and the
console). Clear them any time with `bin/review purge-server preview` and
`bin/review purge-local preview`.

## A second board member's Mac (Lynette)

The console stays local on purpose: plaintext applications exist only on a board
member's Mac, never on a server or in a web page. To give a second person the console:

1. On her Mac: install Node.js LTS from nodejs.org (the Xcode command line tools do not
   include it). Install the Xcode command line tools if `git` asks for them. Then
   `git clone https://github.com/micrui/truckee-cares ~/src/truckee-cares && cd ~/src/truckee-cares && npm install`.
2. `node bin/keygen.mjs lynette-2026`. The label is free-form; `bin/review pull` tries
   every `*.key` in `~/.config/truckee-cares/`. Keygen adds her public recipient to
   `config/season.json`. That change has to reach `main` and get a green "Build and deploy
   site" run **before applications open**: either Mike adds her as a collaborator on the
   GitHub repo and she pushes, or she sends Mike the `age1…` recipient string (it is
   public; a text message is fine) and he commits it. The Worker does not bundle
   recipients, so this needs no `wrangler login` and no worker deploy. From then on every
   application is encrypted to her key too. Applications submitted before her recipient
   was live cannot be opened with her key.
3. Copy the admin token to her Mac: `bin/set-secret admin-token` and paste the value from
   the first Mac's `~/.config/truckee-cares/admin-token`, shared in person or by
   Apple Passwords, never by text or email.
4. Verify: send a test application with `?preview` (or `?preview=<token>` during the
   season), then on her Mac `bin/review pull --season preview`. If it decrypts, she is set.
5. `bin/review pull && bin/review match && bin/review console`. Her local database is
   separate; decisions are shared through the Worker's status field, so agree on who
   decides what.

## Application status for families

A family can check their application on the form's welcome screen ("Check my
application") with the code from their confirmation screen. They see only a status word
and the board's short note: received, we need something from you, approved, not this
year, duplicate, out of area, or replaced. Decisions made in the console reach the status
page after `bin/review push`. To ask a family for something, choose `needs_info` and type
a one-line note in their language with no personal details; the status page shows it with
Text and Call buttons. A family who wants to change something taps "Edit and resend" on the same phone (the
answers come back from an encrypted copy only that phone can open) or fills the form out
again from another phone; either way the new application replaces the old one and
inherits its family and decision in the console.

## Each season

0. Keys. Either `node bin/keygen.mjs season-YYYY` on each board Mac and push the new
   recipients (a fresh key each year limits what one lost key exposes), or keep last
   year's keys and skip this. Either way the backup key and its rehearsal above stand.
1. Edit `config/season.json`: `season`, `opens`, `closes`, `mode: pickup`, clear
   `announcement`. Commit, push, `npm run worker:deploy`. Any change to `season.json`
   needs both: the push rebuilds the site, the deploy updates the Worker's date gate.
   If any dates or text spoken in the form changed, `npm run audio` and commit the MP3s.
2. Import last season if not already: `bin/review import <xlsx> --season 2025`.
3. Clear test rows: `bin/review purge-server preview` and `bin/review purge-local preview`.
4. Pre-open checklist, the day before `opens`:
   - `curl <api_base>/api/status` shows the right `season`, `opens`, `closes` and `mode`,
     and they match `config/season.json`.
   - `<site>/config.json` lists two or more `recipients`.
   - The restore rehearsal line above has this year's date.
   - The Worker was redeployed after the last `season.json` change.
   - A `?preview` submission pulls and decrypts on each board Mac.
5. During the window, every day or two: `bin/review pull && bin/review match`, then
   `bin/review console` and work the task list. Decide on each application:
   accepted, hold, declined, duplicate, out_of_area. `bin/review push` when done.
   `pull` fetches every server row it has not seen, 500 at a time, and reports how many it
   could not read. Those rows never hold up the rest; see "Undecryptable rows" below for
   what they mean and when to delete them. During the window, delete only rows you are
   sure are junk.
6. Early December: Exports → mailing labels and pickup cards. Text accepted families.
7. After distribution: `bin/review purge-server 2026`, then `bin/review purge-server preview`
   and `bin/review purge-local preview` again. Shred CSV exports in Downloads and any
   printed rosters or cards.
8. Retention: the local database keeps this season until next season's matching is done,
   so returning families are recognized. Then `bin/review purge-local <season two years
   back>` (in 2027, purge 2025).

## Changing `mode` (pickup, mail, deliver)

`mode` lives in `config/season.json` and drives the site text, the form's audio, and what
the help assistant tells people. A change takes effect only after both a push to `main`
(site) and `npm run worker:deploy` (Worker). Re-run `npm run audio` if the spoken text
changed, and commit the MP3s.

## If ICE activity makes in-person pickup unsafe

1. Set `mode` to `mail` or `deliver` in `config/season.json` and put a plain notice in
   `announcement` (both languages). Push to `main` and `npm run worker:deploy`. The site
   text and audio, and the help assistant, change only after both have run.
2. In the console, export mailing labels for accepted families and use them for checks or
   gift cards by mail. Text families that pickup is cancelled.
3. Nothing about applicants leaves the review tool.

## If the key file is lost

Without the backup key, or with a backup that was never tested, the applications already
submitted are gone, and with them every way to contact those families. There is no
recovery path: the server holds only ciphertext and never had a key. That is why the
backup key and the rehearsal are mandatory, not advice.

If the backup exists: `bin/set-secret backup-2026.key`, paste the line from Apple
Passwords, then `bin/review pull --reset`. Make a new season key, push the recipient, and
carry on.

If it does not: make a new key, update the recipients, push and redeploy, and ask families
to reapply through the schools and the Family Resource Center.

## If applications were deleted by mistake

D1 keeps up to 30 days of history (7 on the free plan). Restore the database to a moment
before the mistake:

```
npx wrangler d1 time-travel restore truckee-cares --timestamp=<ISO time> --config worker/wrangler.toml
```

Then `bin/review pull --reset`. The Worker refuses to purge the real season while it is
open, so a purge during the window fails on purpose. The `preview` season can be purged
at any time.

## Undecryptable rows

A `decrypt_error` task in the console (or a `decrypt_error` or `bad_payload` line in the
log) means the review tool fetched a row it could not open or read. This is how `pull`
treats such a row: it logs it, opens one task for it (a dismissed task stays dismissed),
adds its id to a local list for the season, and moves on. The pull cursor moves past it,
so one bad row, or ten thousand, never stops the season and is never fetched again by a
plain `pull`. `pull` pages through every row on the server, 500 at a time, until there are
none left, so a flood of junk costs time, not applications.

Two causes, two answers:

- **Key mismatch. Expected and harmless.** The application was submitted before this
  Mac's recipient was in the live site, or this Mac has the wrong key file. Only a Mac
  with the right key can open it; the row is fine and another board Mac probably reads
  it. Nothing to delete. Once the right key is on this Mac (see the second-member steps,
  or the backup key), `bin/review retry-failed --season 2026` fetches just the failed ids
  again and stores the ones that now open; their tasks close on their own.
  `bin/review pull --reset` does the same the slow way (every row again; rows already
  stored are skipped).

- **Junk or spam.** Someone posted rows that look like age files on the outside and are
  garbage inside, or a flood of them. They fail on every Mac, every time. First make
  sure that is what they are: a key mismatch on this Mac lands rows in the same list, so
  check that another board Mac with the season key cannot read them either. Then
  `bin/review delete-failed --season 2026` deletes every id in this Mac's failed list from
  the server after you type the season back. For one row, `bin/review delete-server
  TCC-26-XXXXX` (type the id back), or the "Delete from server" button on its task in the
  console. Deleting is permanent apart from D1 time travel (see "If applications were
  deleted by mistake"). The local list shrinks as each delete lands, so an interrupted
  run can be rerun.

The failed list lives in the local database (`failed:<season>`, at most 5,000 ids per
season) and is this Mac's alone: each board Mac keeps its own. `bin/review purge-local`
clears it with the season.

## Where plaintext exists besides the review database

The review tool is not the only readable copy. CSV exports in Downloads, printed rosters
and pickup cards, and Time Machine backups of a board Mac all hold readable applications.
Shred exports and printouts after distribution. A Time Machine disk is a copy of the
database; store it and lose it with the same care.

## If the Worker is down

The form shows "we could not send" with the phone number. Applicants can text a photo
of a paper form. Enter those in the console by hand once the Worker is back.
