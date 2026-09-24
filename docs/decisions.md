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
