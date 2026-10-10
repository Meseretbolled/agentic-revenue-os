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

**Status: built and tested against a fake SMTP/IMAP server only. Nothing here has run against
real Gmail yet.**

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

## Other services

`revos doctor` checks each with one harmless read and prints one line: `REAL OK`, `REAL FAILED`
(with a category such as `auth`, `timeout`, `network`) or `MOCK` (key missing). It never prints a
value. Of these, **only HubSpot has been run against the real service so far.**

| Service | What doctor calls | Status |
|---|---|---|
| Postgres | `SELECT 1` | seen working locally |
| LLM budget | reads the spend ledger; never calls the model | not run with a live key |
| Slack | `auth.test` | unverified live |
| Gmail | IMAP login and logout only | unverified live (see the Gmail section) |
| Fireflies | GraphQL `{ user { user_id } }` | unverified live |
| Dropbox Sign | `GET /v3/account`; fails if `DROPBOX_SIGN_TEST_MODE` is not true | unverified live |
| Tavily | one search with one result (uses one credit) | unverified live |
| Tunnel URL | `GET <PUBLIC_BASE_URL>/health` | unverified live |

## Commands

```
revos doctor                       # one status per service
revos hubspot-setup                # create the custom properties (safe to repeat)
revos reset-demo --hubspot         # delete ONLY records flagged revos_demo=true (asks first)
make live-gmail                    # sends ONE email to PROSPECT_TEST_EMAIL, checks find_sent
.venv/bin/python scripts/gmail_reply_check.py   # reply by hand; shows the reply fetched, matched, delivered once
make live-hubspot                  # hubspot-setup, then the live tests (one fictional contact/company/deal, cleaned up)
```
