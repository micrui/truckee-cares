# Decisions

Dated. Newest at the bottom. Each one says what was decided and why, so the next person
does not relitigate it without new information.

## 2026-09-24 Static site on GitHub Pages, API on Cloudflare Workers

Wix costs money for eight static pages. GitHub Pages is free and the repo is the site.
The form needs one POST endpoint and a tiny database; a Worker with D1 is free at this
scale and has no server to patch. The domain can move to Cloudflare later; `config/cname`
switches the build from the /truckee-cares subpath to the root.

## 2026-09-24 Encrypt in the browser, server stores ciphertext

The threat is legal process or seizure against whoever hosts the data. Encrypting in the
browser with age to the organization's public key means the host holds nothing readable.
Dedup and eligibility therefore happen after decryption, on a board member's Mac. A few
hundred applications a year makes that trivial. age was chosen over a custom WebCrypto
format because the review tool and the CLI can use the same keys and files.

## 2026-09-24 No server-side family lookup

"Welcome back, Louis" would require the server to answer "is this phone number a client",
which is exactly the query we refuse to make possible. Hashing does not help: the phone
number space is small enough to brute-force. Returning families are recognized on their
own device (localStorage prefill) and, later, through invitation links whose key lives in
the URL fragment.

## 2026-09-24 Matching is a judgment call, so it is a model plus a person

Real duplicates are two adults applying for the same children, with mangled names and
ages that drift a year between seasons. Rules generate candidates and score the obvious
cases; Claude judges the gray zone with the fields a volunteer would look at (no phones
or emails sent); anything unsure becomes a task for a person, with a suggested question
for the applicant. Prior seasons are imported from JotForm exports so continuity (same
family, several years) reads as a trust signal rather than a red flag.

## 2026-09-24 A live LLM in the form is acceptable

Mike's call: live help needs to be immediate, not a callback, and he is not worried about
the model seeing what a person types into a help box. So the form has a help chat and a
"tell us in your own words" mode. Both go through the Worker to the Claude API and store
nothing. The applicant's actual submission still never goes anywhere readable.

## 2026-09-24 Helper mode instead of a helper-phone blocklist

Volunteers and teachers file for several families from one phone. The old script kept a
list of helper phone numbers to ignore. The form now asks "are you filling this out for
someone else?" and records the helper, so the matcher drops the helper's phone from the
family's keys automatically.

## 2026-09-24 Read-aloud uses the phone's own speech engine

For people who read little, every screen has a read-aloud button using the browser's
SpeechSynthesis. It runs on the device and sends nothing anywhere.

## 2026-09-24 Pre-rendered voice, one vendor for static and dynamic speech

The device speech engine on a desktop browser sounded robotic; Mike wants the read-aloud
to be genuinely good for people who read little. Every form screen's text is known at
build time, so it is rendered once with OpenAI's gpt-4o-mini-tts (voice nova, Mexican
Spanish instructions) and shipped as static MP3s: same quality on every phone, works
offline, no third party involved when someone taps play. Only dynamic text (help-chat
answers, the confirmation code) uses the Worker's /api/tts at runtime, with the device
voice as fallback. OpenAI over ElevenLabs and Google because one plain REST call covers
both paths, quality is close, and the whole form costs cents to render.

## 2026-09-24 A microphone button in the form, not the keyboard's dictation key

"Tap the microphone on your keyboard" assumes the person knows that key exists and that
their keyboard has one. The form now records with the browser's own recorder, sends the
clip to the Worker, which transcribes it with OpenAI (gpt-4o-mini-transcribe, language
pinned to the form's language), and then runs the same free-form fill. The transcript
stays visible and editable. Ninety-second cap per recording. Nothing is stored.

## 2026-09-24 Voice-first mode and a listening overlay

The microphone box still left a non-reader with six screens to read. Voice mode asks
seven recorded questions, transcribes each answer, reads it back in the same voice, and
sends with one tap after reading the consents aloud. While the microphone is open, a
full-screen sheet shows a live level meter, a countdown, and one big Done button, so a
person knows the phone is listening and when it is not (three quiet seconds show a hint
to come closer). Playback failures never block the flow: every step advances on end,
error, or blocked autoplay. Verified end to end with synthesized Spanish answers.

## 2026-09-24 Honest disclosure of the AI paths

The site said "your application is encrypted before it leaves your phone and only
volunteers can open it." True of the application. Not the whole story: the microphone,
the help chat, and the "in your own words" box send what the person says or types through
the Worker to OpenAI (speech to text; text to speech for readbacks) and Anthropic (help
answers and free-form fill). Mike accepted that trade on purpose: help has to be
immediate, and a non-reader cannot use the form without voice. Nothing from those calls is
stored by TCC, and the finished application is still encrypted on the phone. The fix is to
say so, in both languages, on the privacy page and the home page, and to scope the
encryption claim to the sent application. Retention got the same treatment: "we delete
applications after distribution day" was only true of the server copy. One board Mac keeps
them until the next season's matching so returning families are recognized; the page now
says that. The features stay. The copy stops overclaiming. CLAUDE.md gains a hard line: no
Workers Logs, Logpush, Tail Workers, or `wrangler tail` during the season, because any of
those would turn the transient path into a stored one.

## 2026-09-24 Preview token during the open season

`?preview` let anyone file test applications into the preview season at any time. Harmless
before opening. During the season it is a way to fill the preview bucket with junk or to
confuse a board member pulling preview rows. So the Worker holds a PREVIEW_TOKEN secret:
while the real season is open, a preview submission needs `?preview=<token>`. Before
opening, plain `?preview` still works, so testing stays easy.

## 2026-09-24 Second recipient is mandatory

One key on one Mac meant a dead disk, a lost laptop, or a wiped user account would lose
every application and every way to contact those families. "Back it up in Apple Passwords"
was advice, never checked. Now a backup identity is generated on the same Mac, its secret
line goes into Apple Passwords, the file is deleted, and config.recipients must list two or
more keys before opening day. The form encrypts to all of them. A restore rehearsal (paste
the backup key on a Mac without the season key, decrypt a preview submission) is on the
pre-open checklist with a dated line in the runbook. A second board member's key is a
third recipient, not a substitute for the backup.

## 2026-09-24 End-to-end audit

Six review agents each read the repo through one lens (form and accessibility, Worker
security, encryption and key path, review tool correctness, operations and the season
calendar, Spanish and tone). Every finding went to a separate skeptic agent told to refute
it; a final critic asked what the lenses missed. Fifty-five agents in all. Forty-four
findings were confirmed and fixed the same day, plus the critic's additions:

- Form: the send path now validates every step on every route (voice and screen); "remember
  on this phone" is off by default, never used for helpers or voice applicants, and the
  welcome card no longer prints a name before a tap; send errors are mapped by cause
  (closed, out-of-date page, busy) instead of "check your connection"; a stale page is
  detected at boot; negated answers ("no, no es la misma", "nada más Diego de 3") no longer
  take the shortcut; each missing field is asked at most twice and then handed to the
  screen; the recording sheet can be cancelled while uploading and the upload times out;
  question audio stops when the microphone opens; reveal inputs no longer scroll the page
  to the top; the ICE fallback now changes the form text and the recordings, not only the
  banner; ?preview is ignored once the season is open unless it carries the token; the form
  shows the phone number instead of "Loading" forever when config cannot load.
- Worker: rate limits keyed on the IPv6 /64 and failing closed; the AI routes only inside
  the season window (or with the preview token) with a size cap on audio and a history cap
  on chat; paginated admin listing; single-row delete; a refusal to purge the open season;
  observability pinned off; the help assistant told it is a program, cannot see
  applications, must not collect personal details, and repeats the board's notice.
- Review tool: one bad row no longer blocks a pull, and the cursor never skips past it;
  any key file in the directory is tried; applicant text is escaped everywhere and the
  console refuses cross-site posts; labels carry the unit; CSVs open correctly in Excel;
  families are merged rather than split when the judge links across pairs; preview rows
  never touch real families; purges leave no dangling tasks.
- Copy and docs: the privacy page and the form say plainly that the microphone, help chat
  and free-form paths send words to OpenAI and Anthropic and that TCC keeps none of it;
  "dirección de correo" became "dirección postal"; the runbook was rewritten so a second
  board member can actually follow it, with a mandatory backup key and a restore rehearsal.
- Public repo hygiene: a real-looking helper phone number and name were replaced in the
  files and scrubbed from git history.

Refuted after tracing the code: that server metadata (timestamps, language, status) could
re-identify applicants against the stated threat model; that the form could encrypt to zero
recipients unnoticed (the library would allow it, so the build and the form now refuse an
empty recipient list); that the key backup story had no
mechanism (it is a file in Apple Passwords, now with a rehearsal step); and that
"comprobante de domicilio" is too bureaucratic (it is the term families already know).
