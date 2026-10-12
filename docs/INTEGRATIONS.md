# Integrations: what was tested, what was seen, what is still unverified

This file records findings from real services. Nothing here is a secret: no token, header or
record value is ever written to it. "Seen" means observed against the real service; "unverified"
means built from documentation and tested only against a fake.

## HubSpot

**Finding (2026-10-11): the service key works as a normal Bearer token.**
`GET /crm/v3/objects/contacts?limit=1` with `Authorization: Bearer <service key>` on
`https://api.hubapi.com` answered **HTTP 200**. The alternative EU base URL
(`https://api-eu1.hubapi.com`) was therefore not needed and not tried. The account is an EU
developer account (the key starts `pat-eu1`), and it still works on `api.hubapi.com`.
`revos doctor` re-checks this on every run and says which base URL to set if the default fails
(401, 403 or 404 on one base triggers a try on the other).

Seen: that one read call. **Not seen yet** (needs `make live-hubspot` with your `.env`):

| Area | Status |
|---|---|
| Create custom properties and the `revos` property group (`revos hubspot-setup`) | built from the docs; tested against a fake; **unverified live**. Needs the schema scopes on the key. |
| Search by `email`, `domain`, `revos_deal_id`, `revos_key` before create | **unverified live** |
| `hasUniqueValue` on `revos_deal_id` and `revos_key` | **unverified live**. If HubSpot refuses it, setup stops with the status and category. |
| Notes: association type ids 202 (contact), 190 (company), 214 (deal) | recalled from HubSpot's documented defaults; **unverified live** |
| Finding a note again by a token in its body (`hs_note_body CONTAINS_TOKEN`) | **unverified live**; used for check-before-retry on notes |
| Search index lag after a write (a few seconds is normal) | handled by looking up to three times before concluding a record is missing; **unverified live** |
| Deleting records with `DELETE /crm/v3/objects/{type}/{id}` | **unverified live** |

### What we write

Every record we create carries `revos_demo = true` and `revos_key` (our idempotency key).
`revos reset-demo --hubspot` deletes only records whose `revos_demo` reads `true`, and checks the
flag again before each delete. Our notes carry the marker `revosdemo`, and the ids of notes we sent
are also taken from our own `actions` table.

Custom properties (created by `revos hubspot-setup`, group `revos`):

* contacts, companies, deals: `revos_demo` (yes/no), `revos_key`
* deals: `revos_deal_id` (unique), `revos_stage`, `revos_score`, `revos_tier`, `revos_score_reason`,
  `revos_outreach_status`, `revos_sequence_day`, `revos_lesson_ids`, and for each of budget,
  timeline, decision maker, signer, needs, objections: `revos_call_<field>` and
  `revos_call_<field>_quote`

### Stage mapping (default deal pipeline)

| Ours | HubSpot stage id |
|---|---|
| new, enriched, scored, outreach, replied | `appointmentscheduled` |
| meeting | `presentationscheduled` |
| qualified | `qualifiedtobuy` |
| proposal, io_approval | `decisionmakerboughtin` |
| out_for_signature | `contractsent` |
| closed_won, handoff | `closedwon` |
| closed_lost, do_not_contact | `closedlost` |

Several of our stages share one HubSpot stage, so the exact one is also stored in `revos_stage`.
`revos hubspot-setup` lists any mapped stage id the account's pipeline does not have.

### Writes are safe to retry

* Contacts are found by email, companies by domain, deals by `revos_deal_id`, before any create.
* A create whose answer was lost (timeout, dropped connection, 5xx) is **not** repeated blindly:
  the adapter looks the record up (by natural key, then by `revos_key`) and only creates again if
  it is really absent. Only a 429, which HubSpot never processed, is retried straight away.
* 429 and 5xx on reads are retried with exponential backoff and jitter, honouring `Retry-After`.
  Other 4xx errors are permanent and are not retried.
* Through the outbox, `find(key)` is the check-before-retry lookup, so a crash between HubSpot
  saving a record and us recording it never produces a second record.

## Gmail (SMTP send, IMAP read)

**Status: send, `find_sent`, reply fetching and header matching have run against real Gmail (owner's
runs). Everything not listed under "What real Gmail did" is tested against a fake SMTP/IMAP server.**

What real Gmail did:

* `make live-gmail` passed: one email sent, and `find_sent` found it again by key.
* `scripts/gmail_header_check.py --no-wait`: **Gmail kept our Message-ID** on the Sent copy, and kept
  our `Reply-To` plus address (`name+d1a2b3c4@gmail.com`). So Gmail does not rewrite the id; the
  real-id read-back is a safeguard that stays a no-op here.
* **`scripts/gmail_header_check.py` passed for real** (owner's run, reply by hand from the Gmail
  web page):
  * our Message-ID was kept by Gmail on the Sent copy;
  * the desktop-web reply carried `In-Reply-To` and `References` with our id, and went to the
    `Reply-To` plus-tag address (`name+<deal tag>@gmail.com`);
  * both the thread-header match and the plus-tag match returned True.
* An **earlier reply from a different client** (Message-ID `<autogen-java-...@google.com>`, a Gmail
  mobile app) had **no thread headers**, and the first `gmail_reply_check.py` run matched it only by
  sender address. That is why the fallbacks exist: the Reply-To plus tag, the stored real Message-ID,
  and the "exactly one open deal" sender rule (anything ambiguous is held, never guessed). The inbox
  also never accepts mail dated before our first sent email.

### How a reply is matched to a deal

All demo leads can share one prospect inbox, so the order matters and nothing is guessed:

1. **Thread**: `In-Reply-To`/`References` name a message we sent (the id we set, or the one Gmail
   stored, kept in `messages.real_message_id`).
2. **Plus tag**: every email has `Reply-To: name+<first 8 chars of the deal id>@gmail.com`. A reply
   to it arrives in the same inbox with the tag in `To`/`Cc`/`Delivered-To`. The tag is never a
   recipient and the recipient allowlist is unchanged.
3. **Sender**: only if exactly one open outreach deal (one that has sent an email) exists for that address.

If a step finds more than one deal, the reply is **held** (table `held_replies`), audited as
`inbound.ambiguous` (ids only, no content) and shown by `revos trace`. It is never attached to a
deal. Follow-ups thread under the id the recipient actually sees.

* **Send** (`smtp.gmail.com:465`, app password). The recipient is the contact row's address, never
  the payload, and must be in the demo allowlist (`PROSPECT_TEST_EMAIL` and the sender). The unsubscribe
  line and `POSTAL_ADDRESS` footer must be in the body or the send is refused. The Message-ID is derived
  from the action's idempotency key, and an `X-Revos-Key` header is a second handle.
* **Retry after a crash**: `find_sent(key)` searches the Sent mailbox by `X-GM-RAW rfc822msgid:`, so
  the outbox finds a mail Gmail already accepted and records it instead of sending again.
* **Threading**: follow-ups set `In-Reply-To` (the message being answered) and `References` (the
  thread so far, oldest first).
* **Replies**: `email.poll` is a scheduled event with one key per poll window. The inbox searches only
  for messages whose `In-Reply-To`/`References` name one of our Message-IDs, or that come from the
  allowlisted prospect. Other mail is never fetched, stored or logged. A delivered reply gets the
  `revos-processed` label (after it was handled), and the search excludes that label.
* **Daily cap**: `daily_cap` (default 20, below Gmail's own limit) is enforced by the send gate.
* **HubSpot from the orchestrator**: stage, score, tier, reason, outreach status, call fields, lesson
  ids and a note for every email sent and call summary. Each write is its own outbox action, so a CRM
  failure never touches an email and a retried email never duplicates a CRM record. A queued CRM
  write that a newer snapshot replaces is cancelled, so an old retry cannot move a stage backwards.

**To confirm with the real service** (`make live-gmail`, `scripts/gmail_reply_check.py`):
Gmail keeps our Message-ID (it may rewrite it, and then the `X-Revos-Key` fallback is used); the
exact `X-GM-RAW` and `HEADER` search syntax; `+X-GM-LABELS` creating the `revos-processed` label;
the folder name `"[Gmail]/Sent Mail"` (it is localised for some accounts); and how long a sent mail takes
to appear in Sent (the live test waits up to 60 seconds).

### What real HubSpot said (live run)

`make live-hubspot` failed on creating a contact with `400 bad_request`. The error did not say why,
so `HubSpotError` now carries HubSpot's own message, its category and the names of invalid
properties (never values, headers or the token; at most 400 characters; a token in a body is
scrubbed). The message was `INVALID_EMAIL` on `email`: **HubSpot rejects `.test` and `.example`
email domains**. The live test now uses `example.com`, and then passed (one contact, company and
deal created and cleaned up). Consequence: a demo lead whose contact email ends in `.example` will
get its contact write refused (a permanent error, in the dead-letter list) while the company and
deal writes still go through; for a real HubSpot run, give demo contacts a valid-looking address
such as the prospect test inbox.

## The IO and Dropbox Sign (test mode only)

**Test mode is enforced in code**: the adapter refuses any request with `test_mode` off before touching the
network, always sends `test_mode=1`, and cancels and refuses a created request whose response does not say
`test_mode: true`. Nothing in the app can send a live request.

What the docs say, and what was **seen live** on the owner's free account:

* Auth is HTTP Basic with the API key as the user and an empty password (`GET /v3/account` answered 200).
* `POST /v3/signature_request/send` with multipart form fields `files[0]`, `signers[0][email_address]`,
  `signers[0][name]`, `metadata[...]`, `test_mode=1` created a request (the docs describe a JSON body but a PDF
  has to travel as multipart; this encoding worked).
* **Free-plan limit seen live**: "In test mode, you can only send emails to your own email address." The signer
  must be the account's own address exactly; a plus-variant of it was refused (403). For the free account the
  signer on the lead record is therefore the account email (the sender inbox, which is on the recipient
  allowlist): `revos seed <tag> --profile hot --signer-email sender`. A paid plan lifts this.
* Metadata is stored as sent. Searching `metadata:<value>` finds a request but **the search index lagged**
  (not found for about a minute); the plain list shows a new request at once, so the adapter checks both.
  Even so "not found" is not proof it was never created, so it is treated as "cannot tell".
* Callbacks (per the docs): multipart/form-data with the event in a field named `json`; `event.event_hash` is
  HMAC-SHA256 (hex) over `event_time + event_type` keyed with the API key; the receiver must answer HTTP 200 with
  a body containing `Hello API Event Received` or the delivery is resent. The docs do not describe a
  `callback_test` event beyond the UI's test button, so a verified event of that type is acknowledged and ignored.
  The hash covers only the time and the type, not the signature request id, so the id in an event is not
  authenticated by it: the deal closes only when the provider's own API says the request is complete.
* The account callback URL is set by hand: Dropbox Sign account, Settings, API, Account callback:
  `PUBLIC_BASE_URL/dropboxsign/webhook`. (On the owner's account it was unset.)

How the IO is built and sent:

* **Only for a deal at `qualified`.** Fields: advertiser, signer name, title and email (the email from the lead
  record's `signer_email`, on the allowlist; never from a call or a payload), the budget with its call quote,
  term, start date, discount and payment terms. Validated in code: required fields, prices against the SAMPLE rate
  card, the budget inside the pack's bounds, a maximum term, and the discount. **A discount over the pack
  maximum is a hard block that never sends and cannot be overridden by anyone** (the earlier "manager approval"
  override was removed).
* The IO is rendered to a PDF (fpdf2, no network), attached alone, and sent in test mode. The action follows the
  pack's autonomy level (the Slack veto when `auto_with_veto`).
* **Check before retry**: the attempt is written to `esign_attempts` on its own connection before the create call,
  our key goes in the request metadata, and the signature request id is kept in the outbox. Before any retry the
  provider is asked about our key; if it cannot be told whether a request was created the action goes to review
  (`esign.review_required`) and is **not** sent again.
* **Webhook** `POST /dropboxsign/webhook`: verified exactly as above (401 otherwise, audited without the body),
  deduplicated by `signature_request_id + event_type + event_time`, sent/viewed/signed audited, and
  `signature_request_all_signed` closes the deal through `transition_deal()`, updates HubSpot (stage,
  `closedate`, `amount`, `revos_signature_request_id`, a note with the id) and posts the handoff to Slack.
* Not built: the fallback signing page, because the real API and callback could be used on the owner's account.

## Fireflies (call intelligence)

**Status: the real GraphQL adapter is built and tested against a fake server; the live calls are described
under "What ran live" once the owner's run is recorded. `revos call-demo` runs a scripted call offline.**

* **Adapter**: `https://api.fireflies.ai/graphql`, `Authorization: Bearer <FIREFLIES_API_KEY>`. It lists recent
  transcripts (id, title, date; no content) and fetches one (title, date, participant emails, speaker-labelled
  sentences, Fireflies' overview). 429 and 5xx are retried with backoff (a `Retry-After` is honoured); any other
  client error is permanent; errors carry Fireflies' error code and nothing else. The key is never in an error or a repr.
* **Completion: polling only.** `calls.poll` runs every five minutes and is idempotent per transcript id. There is
  **no `/fireflies/webhook`**. Fireflies' docs say a webhook carries an `x-hub-signature` header, "a SHA-256 HMAC
  signature of the request payload", but do not say whether it covers the raw body or a parsed form, nor how the
  signature is encoded, and say nothing about the free plan. Without that the scheme would be guesswork, so it is
  not implemented; a test asserts the route does not exist.
* **Matching a call to a deal**, in this order, each rule narrowing the candidates only if one survives: a
  participant email equals the contact (an exact address beats a plus-variant of the same inbox); the title contains
  the deal's short id or the company name; the call started within three hours of the booked meeting. Exactly one
  deal left means a match; more than one, or no evidence at all, means the call is **held** (table `held_calls`,
  audit `call.unmatched`, shown by `revos trace --recent`) and never guessed.
* **Untrusted**: the transcript is wrapped as untrusted text and scanned; one containing an instruction is quarantined
  (no field, no lesson, the model never sees it) and goes to a person.
* **Extraction** (budget, timeline, decision maker, signer, needs, objections, competitors, next steps): every value
  carries a verbatim quote, checked in code; a value whose quote is not in the transcript is dropped and audited
  (`call.field_dropped`). A signer NAME is all a call can give: the signer's address comes from the lead record
  (`signer_email`, seeded as a fictional plus-address) and the recipient allowlist applies to it; an agent cannot
  write that field.
* **After a call**: lessons are written with their evidence quote (modest starting confidence), the qualification
  gate runs and moves `meeting -> qualified` only through `transition_deal()` (or says what is missing), HubSpot gets
  the call fields, a note with the summary and lesson ids and the stage, and Slack gets a short summary.
* `revos hubspot-setup` creates the new properties (`revos_call_competitors`, `revos_call_next_steps` and their quotes).
* `revos calls list` shows held calls; `revos calls assign <transcript id> <deal id>` is how a person decides which
  deal a held call belongs to (audited as `call.assigned`, then processed like a matched call, once).

What ran for real (owner's account): `revos doctor` shows Fireflies `REAL OK`; the adapter listed the owner's one
transcript ("Harborview Optical Collective - intro call.m4a", no participants, one speaker). The title matched seven
Harborview demo deals, so the call was **held, not guessed**; it was then assigned by hand to one deal and processed:
four fields with verbatim quotes (budget, timeline, decision maker, objections), a lesson ("CPM objection: lead with
ROAS evidence", confidence 0.50), the HubSpot deal fields and a call-summary note, and a Slack summary. The extraction
there ran on the offline rules model (`LLM_MODE=fake`), so it found what simple phrase rules find and nothing more
(for example the timeline field is the sentence about the brand team's approval time, not a launch date). The deal was in
`replied`, not `meeting`, so the qualification gate did not run.

## Slack (the veto window, approvals, the digest)

**Status: the real adapter has posted one message and updated it in place (`revos slack-test`). The signed
interactions endpoint, the buttons and the digest are tested offline against a mock Slack; the live click
test needs the tunnel (below).**

What it does:

* Every outbound action decided `auto_with_veto` is posted to `SLACK_CHANNEL` as a Block Kit message: what will
  be sent and to whom (the lead's name and company, never the address), the score and tier with the written
  reason, the case study, the lesson ids, the claim-check result, and "Sends in 60 seconds" with a **Cancel**
  button. When the window ends the same message is updated (`chat.update`) to "Sent <time>" or "Cancelled by
  <user>".
* Actions that need approval (approval mode, or a `needs_approval` level) show **Approve** and **Reject**.
  Hard-blocked actions are posted as "Blocked: <reason>" with no button, and a forged approve for one is
  refused and audited.
* `POST /slack/interactions` verifies Slack's v0 signature over the raw body (HMAC-SHA256, constant-time, five
  minutes of clock tolerance), answers 401 (and logs without the body) otherwise. A button carries only the
  action id: the action's state and whether the press is allowed are re-read from Postgres. A cancel is a
  compare-and-set under the action's row lock, so it and the send at the end of the window cannot both win.
  The same press delivered twice (same trigger id) does nothing the second time and still answers 200.
  `SLACK_ALLOWED_USERS` limits who may press buttons; others get an ephemeral refusal and an audit row.
* `tracking.digest` also posts a daily summary (leads by stage, emails sent, replies, vetoes, held and deferred
  sends, waiting for approval, dead letters, held replies, new lessons, deals stalled), once per day.

Slack app settings (api.slack.com/apps):

1. **OAuth & Permissions, Bot Token Scopes**: `chat:write` (post and update). `chat.postEphemeral` needs no
   extra scope. Add `channels:read` only if you later want the app to look channels up by name; it does not now.
   Install the app to the workspace and copy the **Bot User OAuth Token** (`xoxb-...`) into `SLACK_BOT_TOKEN`.
2. **Invite the bot to the channel** (`/invite @YourApp` in `SLACK_CHANNEL`); without it Slack answers
   `not_in_channel`.
3. **Basic Information, Signing Secret** into `SLACK_SIGNING_SECRET`.
4. **Interactivity & Shortcuts**: turn on Interactivity and set the **Request URL** to
   `PUBLIC_BASE_URL/slack/interactions`.
5. `PUBLIC_BASE_URL` is the https URL of a tunnel to port 8100, for example
   `cloudflared tunnel --url http://localhost:8100` (it prints a `https://<your-tunnel>.trycloudflare.com` URL; a quick
   tunnel's URL changes each time, so update `PUBLIC_BASE_URL` and the Request URL when it does).
   `revos doctor` calls `GET PUBLIC_BASE_URL/healthz` through the tunnel, and says plainly when it is unset:
   "Slack buttons will not work".

Commands: `revos slack-test` posts one test message and updates it in place.

## Running the system by itself (`revos run`)

`revos run` starts the API (port 8100), the worker and the scheduler in one process and prints a
banner with REAL or MOCK for each integration. The scheduler enqueues `email.poll`,
`tracking.stall_scan`, `tracking.digest`, `lessons.sweep` and `queue.release_held` (one key per time
bucket, so two schedulers never duplicate). Ctrl-C finishes the job being run; a job that cannot
finish within 15 seconds goes back on the queue with its attempt returned. Nothing is lost, and the
outbox idempotency keys make a re-run safe. Before it starts it runs the doctor checks: a service
that will run as REAL and fails stops the start (`--force` overrides, the failure is logged).

* **Real by default**: keys in `.env` select the real adapter, missing keys select the mock. With a
  key present and the service down the real adapter raises (jobs retry, doctor fails); there is no
  fallback to a mock. `revos doctor` prints `will run as: REAL|MOCK` per service.
* **Mock unless switched on**: Tavily. An adapter exists (see "Real web research" below) but `ENRICHMENT_MODE` defaults to `mock`, so research runs on the offline mock. While the mode is `mock`, for this service, `revos doctor` and the banner say `key OK, adapter not built yet:
  runs as MOCK` (or `key NOT working (...)`) instead of `REAL OK`.
* **Demo leads are plus-addresses** of `PROSPECT_TEST_EMAIL` (`name+acme@gmail.com`): each is a
  distinct HubSpot contact (HubSpot merges by email and refuses `.test` and `.example`), and every
  email lands in the one inbox. The demo-mode recipient rule accepts an address only if its domain
  is the base address's and its local part is exactly the base local part, optionally followed by
  `+tag`. `name2@`, `name.x+tag@`, `name+x@other.com` and `name+a+b@` are all refused.
  `revos seed <tag>` creates one such lead.
* **Reply matching with plus-addressed leads**: the reply comes *from the base address*, so the
  sender alone cannot say which lead it answers. Thread headers (In-Reply-To/References) identify the
  lead when the mail client keeps them; the `Reply-To` plus tag (`sender+<deal id prefix>@`) identifies
  it when it does not; the sender rule compares base addresses and accepts only one open deal.
  Covered offline by `tests/integration/test_plus_addresses.py`; which of the two a real client
  provides was recorded by `scripts/gmail_header_check.py` (see above): the web client provides both.
* **Correlation id**: one id is created with each event and follows it through the job, the agent
  runs, the policy decision, the outbox action, the audit rows and every log line.
  `revos trace <deal id>` (or `--lead <email>`, `--json`) merges events, agent runs and proposals, policy
  decisions, outbox and CRM actions, emails, held replies, lessons and audit rows;
  `revos trace --recent` is the old audit listing. Bodies are never shown.
* **Logs**: JSON lines in `var/logs/revos.jsonl` (ts, level, component, deal_id, correlation_id,
  message). Anything that looks like a key, token, app password or Authorization header, and the exact
  value of every secret-looking variable in the environment, is replaced by `[redacted]` before it is
  written. `revos logs --deal <id>` filters the file.

### The live end-to-end run (owner's machine, real HubSpot and Gmail)

`revos run` and `revos seed acme` were run for real. For the plus-addressed lead the system enriched,
scored (tier C, 38/100) and wrote the contact, company and deal to the real HubSpot portal, all under
one correlation id (`revos trace` shows the whole timeline). The first email was **held by the send
window**, not sent: the pack only allows weekdays 09:00-17:00 in the recipient's timezone, and the run
was on a Sunday. It is queued as a timer for Monday 09:00 UTC and `revos run` sends it then. Nothing was
changed to get around the window. The first real email from `revos run` has therefore not been seen yet.

### Send window: `SEND_WINDOW_MODE`

The pack's window (weekdays 09:00-17:00 in the recipient's timezone) is the default (`enforce`).
`SEND_WINDOW_MODE=demo_bypass` skips only that check, only when `REVOS_ENV=demo` (anything else is a
startup error, in `revos run`, `revos doctor` and the composition root). While it is on, the banner and
`revos doctor` say `SEND WINDOW BYPASSED: demo only`, and each send it lets through writes a
`send_window.bypassed` audit row with the reason. The recipient allowlist, caps, suppression, the kill
switch and hard blocks are untouched (there is a test for each).

### How each email is produced (`LLM_MODE=fake` vs `live`)

Code (the pack's `messaging.yaml` templates and snippets) writes the greeting, the product wording, case
studies, meeting times, the sign-off, the unsubscribe line and the postal address. The model, when there is
one, writes ONE short paragraph per email and nothing else.

| Email | Template (code) | Model-written paragraph | Fallback if a check fails |
|---|---|---|---|
| first touch | greeting, pillar wording, case study, ask, footer | personal opening from the lead's sourced facts, tone rules, cited lesson ids | `first_touch_fallback` snippet |
| reply / meeting times | "Thanks for getting back to me, Maya.", slot list (code, in the prospect's zone), intro, footer | the acknowledgement, written from the (fenced) reply text | the `reply_ack_*` snippet |
| follow-up | greeting, pillar wording, footer | personal opening | `follow_up_fallback` snippet |
| confirmation | all of it (exact time + invite) | none, by design | n/a |
| case-study answer | all of it (stored facts only) | none, by design | n/a |

`fake`: the deterministic `FakeLLM` writes the paragraph from the first fact (no key, no network).
`live`: the Claude Agent SDK writes it (Sonnet for drafting, Haiku for classification, a spend-capped
ledger reserving before every call). Checks on the model's text, in order: the injection scanner on what it
wrote, the footer (unsubscribe line and postal address still in the finished email), the deterministic claim
checker (every number, date and name must be in the cited evidence or approved wording), then a model
verifier for meaning. Up to two regenerations with the issues as feedback; if a CHECK still fails the email is
rebuilt from the pack's wording and a `draft.fell_back_to_template` audit row records which checks failed.
A model error, a spend-cap stop or an unavailable verifier is not papered over: the run goes to a person.
The Gmail mailer and the send gate still enforce the footer and the recipient allowlist on whatever is sent.

### Per-domain cap: `DOMAIN_CAP_MODE`

The pack's per-domain daily cap is the default (`enforce`). Because every demo email goes to one domain,
`DOMAIN_CAP_MODE=demo_bypass` skips ONLY that cap, only when `REVOS_ENV=demo` (otherwise `revos run`,
`revos doctor` and the composition root refuse to start), and says `DOMAIN CAP BYPASSED: demo only (all
demo mail goes to one domain)`. Each send it lets through writes a `domain_cap.bypassed` audit row. The
overall daily cap, allowlist, suppression, kill switch, send window and hard blocks are untouched.

`revos queue retry-held [--deal ID]` asks for sends held by the send window or a cap to be tried again
now. Each goes through the same gate, so every check runs again; it is audited as `operator.retry_held`
and never sends twice (outbox idempotency).

### Seed profiles

`revos seed <tag> --profile hot|warm|cold` takes the lead's data from `revos/fixtures/seed_profiles.yaml`
(fictional, synthetic CRM signals; no reply or meeting is faked). With the sample pack they score
A (83/100), B (51/100) and C (23/100) from the pack's own weights, each with a per-rule written reason.

Each profile also has the contact's IANA `timezone` and two `research_notes`: invented facts about
the fictional company, each with a `source` that must start `fictional demo profile`. Enrichment stores
them as sourced facts (`fact:DEMO1`, `fact_source:DEMO1`) after any real research (which comes first
and takes priority), the first email cites one through the claim checker and prints its source label,
and the trace says they are fictional demo data. They are never presented as real research.

### How the emails read

* The meeting email acknowledges the reply ("Thanks for getting back to me, Maya." plus a sentence for
  the reply's class, from the pack's `messaging.yaml` `snippets`) and then gives the times; no model writes it.
* Slots are chosen inside the prospect's working hours (the pack's send-window hours) in their time
  zone, on the weekday they named if one is free, and shown as `Thursday 15 October, 09:00 (your time, UTC-5)`
  with the offset for that date (daylight saving applies). The data (actions, `offered_slots`, the invite,
  the `slots.offered` audit row) stays in UTC. With no valid time zone the email says so and shows UTC, and
  a `slots.timezone_unknown` audit row is written. A reply naming a local time is matched in that zone.

## Other services

`revos doctor` checks each with one harmless read and prints one line: `REAL OK`, `REAL FAILED`
(with a category such as `auth`, `timeout`, `network`) or `MOCK` (key missing). It never prints a
value. In the owner's last run Postgres, HubSpot, Slack, Gmail, Fireflies and Dropbox Sign were `REAL OK`. Tavily research is off by default (`ENRICHMENT_MODE=mock`); see "Real web research" below for its state.

| Service | What doctor calls | Status |
|---|---|---|
| Postgres | `SELECT 1` | seen working locally |
| LLM budget | reads the spend ledger; never calls the model | not run with a live key |
| Slack | `auth.test`; also notes a missing `SLACK_SIGNING_SECRET` | REAL OK; a real post and in-place update succeeded |
| Gmail | IMAP login and logout only | REAL OK (owner's run) |
| Fireflies | GraphQL `{ user { user_id } }` | REAL OK; the transcript adapter is built (see the Fireflies section) |
| Dropbox Sign | `GET /v3/account`; fails if `DROPBOX_SIGN_TEST_MODE` is not true | REAL OK; a test-mode request was created and cancelled live (see its section) |
| Tavily | `POST https://api.tavily.com/search` with `Authorization: Bearer <key>` and a JSON body (one credit) | adapter built, off by default; **the API answered `403 Forbidden` (an HTML page from the CDN) for the owner's key when the adapter was built, while an invalid key got a JSON 401, so it has never run live** (see "Real web research") |
| Tunnel URL | `GET <PUBLIC_BASE_URL>/healthz` | unverified live |

## Commands

```
revos doctor                       # one status per service
revos hubspot-setup                # create the custom properties (safe to repeat)
revos trace                        # recent audit entries, and replies held as ambiguous
.venv/bin/python scripts/gmail_header_check.py  # prints Message-ID/In-Reply-To/References/To of the Sent copy and your reply
revos reset-demo --hubspot         # delete ONLY records flagged revos_demo=true (asks first)
make live-gmail                    # sends ONE email to PROSPECT_TEST_EMAIL, checks find_sent
.venv/bin/python scripts/gmail_reply_check.py   # reply by hand; shows the reply fetched, matched, delivered once
make live-hubspot                  # hubspot-setup, then the live tests (one fictional contact/company/deal, cleaned up)
```

## IO terms are proposed automatically

When the qualification gate moves a deal to `qualified`, the IO planner (`revos/skills/io_terms.py`) proposes
the terms from the call fields and the pack's SAMPLE rate card, then `validate_io` checks them in code:

- **Package**: the largest package whose `min_monthly_budget_usd` the stated monthly budget reaches (a budget
  with no stated period is not guessed; a person is asked).
- **Start date**: the first Monday on or after today + `io_min_lead_days`; a timeline too tight for that needs a person.
- **Term**: `io_default_term_months`. Quantities are PER MONTH (`location_count` capped by the pack); the document
  multiplies by the months: monthly subtotal x months = term total.
- **Campaign media**: the package's `media_item` takes the rest of the monthly budget, so the monthly subtotal sits
  between the package minimum and the stated budget (SAMPLE numbers: Scale at $40,000 a month = $9,900 of service
  lines + $30,100 media = $120,000 over 3 months).
- **Validated in code**: the net monthly subtotal must reach the package minimum and not exceed the stated monthly
  budget (a quarterly or yearly budget is converted); either failure blocks the IO.
- **Discount**: 0 unless the pack's `volume_discounts` say otherwise. Over the pack maximum is a hard block, no override.
- **A person** is involved only when a term falls outside the pack's limits (`io.terms_need_a_person` audit).
- `revos io-terms` stays as an override; it is audited as `deal.io_terms_set` with source `override` (planner: `auto`)
  and the planner never replaces terms already set.

## Live model checks (LLM_MODE=live)

- `scripts/sdk_smoke.py`: one Outreach agent call through the Agent SDK with its scoped tools (cap `SMOKE_MAX_USD`).
- `scripts/live_draft.py`: one first-touch draft with the claim checks (cap `LIVE_DRAFT_MAX_USD`).
- `scripts/live_check.py`: replies (interested, objection, two injections), extraction from the Harborview call with the
  exact-quote check, and a first-touch draft; every call listed with model, tokens, cost and running total (cap
  `LIVE_CHECK_MAX_USD`, default $0.50).
- `make live-llm`: one whole lead with the real model against the mock mail, CRM and e-sign adapters (cap
  `LIVE_LEAD_MAX_USD`). Measured about $0.05 a lead.
- `revos doctor` prints an `LLM spend` line: calls, tokens in/out and dollars against `LLM_MAX_SPEND_USD`.

## The operator dashboard

Open `http://127.0.0.1:8100/dashboard/` (the API binds to 127.0.0.1 only). Pages: Pipeline board, deal page, Activity,
Pending (veto countdown, approvals, hard blocks), Queue and dead letters, Lessons, Integrations, Controls.

- **Sign-in:** set `DASHBOARD_TOKEN` (any long random string) in your local settings file and restart `revos run`.
  With no token the dashboard answers 503. The token is compared in constant time, the session is a signed HttpOnly
  SameSite=Strict cookie, and every POST needs a CSRF token. Five wrong tokens lock a client out for five minutes.
- **Never public:** every `/dashboard` route is refused (404) for a request that arrives through the public tunnel, by
  the same allowlist that keeps `/status` off it. Only the Slack and Dropbox Sign webhooks and `/healthz` are public.
- **Actions** call the system's own code: veto, human approval, dead-letter retry and the settings setters, so the same
  locks and audit rows apply (actor `dashboard`). The dashboard never writes a deal stage and sends nothing itself.
- **Controls** (kill switch, autonomy mode, veto seconds) live in `system_settings`, override the environment, are read
  by the policy engine on every decision and are audited. The page shows each effective value and where it comes from.
- `revos dashboard-seed` inserts clearly marked demo deals (rows only; nothing is queued or sent); `--clear` removes them.
- Screenshots: `docs/screenshots/dashboard-*.png`.

## What HubSpot shows about a deal

- **Owner:** new and ownerless deals and contacts get the seller as owner (never overwriting a person's
  reassignment). Pick the seller with `revos hubspot-owners`, then set `HUBSPOT_OWNER_ID` (or `HUBSPOT_OWNER_EMAIL`).
  With exactly one HubSpot user that user is used; with several and no choice nobody is assigned.
- **Score:** `revos_score`, `revos_tier` and `revos_score_reason` (every rule's written reason) are deal properties, and
  the same text is a note on the deal ("Lead score 83/100, tier A ..."), written once per distinct score.
  The properties live in the "Revenue OS" group; to see them on the deal card open the deal, Customize, About this deal,
  and add that group (HubSpot decides card layout, the API cannot).
- **Emails:** every sent and received email is logged on the contact's and the deal's timeline (an Emails engagement,
  keyed by its Message-ID, so a retry logs nothing twice). HubSpot computes Last Contacted itself from logged emails:
  it is read-only for us.
- `revos hubspot-backfill [--deal ID]` queues all of that for existing deals; it is safe to repeat.

### Scopes the HubSpot service key needs

| What | Scope |
|---|---|
| Owners (list, assign) | `crm.objects.owners.read` |
| Log and look up emails | `sales-email-read` (HubSpot accepts it instead of `crm.objects.emails.read`; verified live), plus write access to emails |
| Notes | `crm.objects.contacts.write`, `crm.objects.deals.write` (already working) |
| Custom properties (`revos hubspot-setup`) | `crm.schemas.contacts.write`, `crm.schemas.companies.write`, `crm.schemas.deals.write` (already working) |
| Records | `crm.objects.contacts.read/write`, `crm.objects.companies.read/write`, `crm.objects.deals.read/write` |

Verified live with `crm.objects.owners.read` and `sales-email-read` added: owners list, email lookup and email
creation all work. Emails are found by `hs_email_message_id` (HubSpot does not allow searching the email text). Email jobs that hit the missing read scope wait in the dead
letters: add the scope, then press Retry on the dashboard's Queue page (or `revos queue retry <id>`).

## Real web research (Tavily), `ENRICHMENT_MODE=mock|tavily`

Default `mock`: nothing below runs and enrichment is exactly as before. With `ENRICHMENT_MODE=tavily` (needs
`TAVILY_API_KEY`, or `ENRICHMENT_CACHE=replay` to answer only from the recorded cache with no network and no key):

* The enrichment agent searches the web for the lead (`{company} {domain}`), through the same scoped, fenced and
  scanned tools as before. A result is accepted **only if** its page is on the lead's own domain, it names the
  company (kind-of-company words such as "media" or "group" do not count; they live in
  `revos/fixtures/generic_name_words.yaml`), and, when the lead has an `hq_location`, it names that place
  (`revos/skills/entity_match.py`). Every other result is rejected and audited as `enrichment.rejected_result` with
  the reason. A page can be fetched only if the search returned it. The pack's own `enrichment.yaml` extractors read
  the accepted text, so no code names a segment; the call caps (`web.max_searches`, `web.max_page_fetches`) default
  to 1 search and 2 page fetches per lead.
* A fact is stored as `fact:W<n>` with `fact_source:W<n>` (its URL) and `fact_fetched:W<n>` (the fetch time). A
  `W` fact missing either is not citable, so a draft cannot use it (the claim checker sees it as unknown evidence).
* Every answer is cached (`var/cache/tavily/`, one JSON file per request, with its fetch time and credits); a
  repeat costs nothing and replay works offline. `TAVILY_MAX_CREDITS` (default 20) caps credits per process.
* A failing, capped or empty search falls back to the offline mock and is audited as
  `enrichment.fell_back_to_mock` with the reason; the run continues.
* **Nothing is ever sent to a researched company.** Leads use the owner's allowlisted inbox; `seed_lead` refuses an
  address at the company's domain and the send gate's recipient allowlist refuses any other.
* Real companies for the demo are in `revos/fixtures/real_seed_profiles.yaml` (`revos seed <tag> --real orangetheory`
  or `--real hawkemedia`); the existing seed profiles are untouched. `revos enrich <company> --domain <domain>
  [--location ...] [--replay]` prints accepted facts (source URL, fetch time) and rejected results (reason).

**State, plainly:** all of this is tested offline with SYNTHETIC fixtures (`tests/fixtures/tavily/README.md`). It has
never run against the live API: when it was built, the owner's key got `403 Forbidden` (an HTML page from the
CDN, not Tavily's JSON) while an invalid key got a JSON `401`, so the key looks blocked or revoked at the edge.
Tavily's real answer shape (`results[].content`, `usage.credits`, `/extract` `raw_content`) is taken from its
documentation and is unverified. The pack extractors are SAMPLE rules: they do not know "studios" or "fitness", and
the agencies pack's `enriched` exit criterion needs `client_count`, so a real company may stop at that stage until
the lead record or the extractors are extended.
