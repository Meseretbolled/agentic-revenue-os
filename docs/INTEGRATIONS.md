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
   `cloudflared tunnel --url http://localhost:8100` (it prints a `https://....trycloudflare.com` URL; a quick
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
* **Not built yet, so MOCK even with a key**: Fireflies (Step 11), Dropbox Sign
  (Step 12), Tavily. For these, `revos doctor` and the banner say `key OK, adapter not built yet:
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
value. In the owner's last run Postgres, HubSpot, Slack, Gmail, Fireflies and Dropbox Sign were `REAL OK`; Tavily failed (see below).

| Service | What doctor calls | Status |
|---|---|---|
| Postgres | `SELECT 1` | seen working locally |
| LLM budget | reads the spend ledger; never calls the model | not run with a live key |
| Slack | `auth.test`; also notes a missing `SLACK_SIGNING_SECRET` | REAL OK; a real post and in-place update succeeded |
| Gmail | IMAP login and logout only | REAL OK (owner's run) |
| Fireflies | GraphQL `{ user { user_id } }` | REAL OK (owner's run) |
| Dropbox Sign | `GET /v3/account`; fails if `DROPBOX_SIGN_TEST_MODE` is not true | REAL OK (owner's run) |
| Tavily | `POST https://api.tavily.com/search` with `Authorization: Bearer <key>` and a JSON body (one credit) | our call matches Tavily's documented shape; the owner's key was answered 403, which doctor now reports as `key rejected` (check or regenerate the key) |
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
