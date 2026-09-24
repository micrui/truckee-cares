# Runbook

## One-time setup

1. **Key.** `node bin/keygen.mjs season-2026` writes the private key to
   `~/.config/truckee-cares/season-2026.key` and adds the public recipient to
   `config/season.json`. Back the key file up in Apple Passwords or 1Password now.
   Losing it loses every application. To let a second board member decrypt, run keygen
   on their Mac and add their recipient to the list too (before applications open).
2. **Worker.** `npx wrangler login`, then:
   ```
   npx wrangler d1 create truckee-cares            # paste database_id into worker/wrangler.toml
   npx wrangler d1 execute truckee-cares --remote --file worker/schema.sql --config worker/wrangler.toml
   npx wrangler secret put ADMIN_TOKEN --config worker/wrangler.toml       # long random string
   npx wrangler secret put ANTHROPIC_API_KEY --config worker/wrangler.toml # optional: live help
   npm run worker:deploy
   ```
   Put the Worker URL in `config/season.json` as `api_base`. Save the admin token to
   `~/.config/truckee-cares/admin-token` (mode 600).
3. **Voice.** `bin/set-openai-key` opens a dialog and stores the OpenAI key in
   `~/.config/truckee-cares/openai-key`. Then `npm run audio` renders one MP3 per form
   screen per language into `site/static/audio/` (about 5,600 characters, a few cents).
   Commit the MP3s. Re-run after changing form text; only changed screens re-render.
   For spoken help answers, also `npx wrangler secret put OPENAI_API_KEY --config worker/wrangler.toml`.
4. **Site.** Push to `main`. GitHub Pages builds from the Actions workflow. Custom domain:
   write it into `config/cname`, add the DNS record, and push.

## Testing before the season opens

Open the form with `?preview` on the end. It is always open, shows a "preview mode"
banner, and stores submissions under the `preview` season with codes like `TCC-PV-…`.
They go through the real path: encrypted, stored, pulled and matched in the review tool
(`bin/review pull --season preview`, then match and the console). Clear them any time:
`bin/review purge-server preview` and delete them from the local database.

## Each season

1. Edit `config/season.json`: `season`, `opens`, `closes`, `mode: pickup`, clear
   `announcement`. Commit, push, `npm run worker:deploy`.
2. Import last season if not already: `bin/review import <xlsx> --season 2025`.
3. During the window, every day or two: `bin/review pull && bin/review match`, then
   `bin/review console` and work the task list. Decide on each application:
   accepted, hold, declined, duplicate, out_of_area. `bin/review push` when done.
4. Early December: Exports → mailing labels and pickup cards. Text accepted families.
5. After distribution: `bin/review purge-server 2026`. Keep the local database until the
   next season's matching is done, then delete applications older than two seasons.

## If ICE activity makes in-person pickup unsafe

1. Set `mode` to `mail` or `deliver` in `config/season.json` and put a plain notice in
   `announcement` (both languages). Push and deploy. The site and the help assistant pick
   it up.
2. In the console, export mailing labels for accepted families and use them for checks or
   gift cards by mail. Text families that pickup is cancelled.
3. Nothing about applicants leaves the review tool.

## If the key file is lost

Applications already submitted cannot be recovered. Generate a new key, update the
recipients, deploy, and ask families to reapply. This is why step 1 says back it up.

## If the Worker is down

The form shows "we could not send" with the phone number. Applicants can text a photo
of a paper form. Enter those in the console by hand once the Worker is back.
