# Agentic Revenue OS

AI agents that run the revenue lifecycle for an ad-tech company: **prospect → enrich → score → outreach → reply →
meeting → qualify → IO → e-sign → handoff**. HubSpot is the system of record, Postgres is the source of truth, and the
agents are built on the Claude Agent SDK (Python 3.12). It is an interview project shown as a recorded demo, and it runs
both against real services and fully offline with mocks.

## The idea in one paragraph

**The model proposes, code decides.** Agents only return proposals. A policy engine (an allowlist with hard blocks)
and a Postgres state machine decide what happens. Every side effect goes through an outbox with a unique idempotency
key, and the send step re-reads the approval from the database, so a flag passed by a caller is never trusted. Inbound
text (replies, web pages, transcripts) is untrusted data: it is wrapped, scanned and quarantined, never obeyed. Facts
in a draft must each cite evidence and pass a deterministic claim check, and a model-written line that fails a check is
replaced by the segment's own approved wording.

## What is real, what is mocked, what is test mode

| Area | Status |
|---|---|
| Claude model (drafting, reply labels, call extraction) | Real with `LLM_MODE=live` (spend-capped); offline rules model otherwise |
| HubSpot (deals, contacts, owner, score note, email timeline) | Real when its key is set |
| Gmail (SMTP send, IMAP replies) | Real when set; mail goes only to the owner's own addresses in demo mode |
| Slack (veto window, approvals, digest, drafts) | Real when set; buttons need a public tunnel |
| Fireflies (call transcripts) | Real adapter, polled (every 300 s by default; `CALLS_POLL_SECONDS` shortens it, minimum 30 s) |
| Dropbox Sign | **Test mode only**; nothing is legally binding |
| Tavily (web research) | **Not built.** The key works, but there is no Tavily adapter: all research runs on the offline mock |
| LinkedIn | **Draft-only**. No code can send to LinkedIn: a person copies the text |
| Case studies | Public testimonials published on cool.co, each with its source link and label |
| Rate card | A **SAMPLE**, labelled as one. Not COOL's prices |
| Demo leads | Fictional. All mail goes to plus-addresses of one prospect inbox |

`revos doctor` prints REAL OK / REAL FAILED / MOCK for every service and what it will run as. A real adapter is used
whenever its keys exist; the app never falls back to a mock silently. See `docs/INTEGRATIONS.md` for what was run live
and what was not.

## Architecture

```
domain  ←  ports  ←  skills / policy  ←  orchestrator / agents  ←  adapters / api
```

Only the composition root (`revos/composition.py`) wires concrete adapters, and every real adapter has a mock with the
same interface. Rules that are never broken:

1. Agents have no built-in SDK tools and load no host settings. They get only their own scoped, read-only MCP tools,
   and an unresolvable scope blocks the call (fail closed).
2. A deal's stage changes only through `transition_deal()`, which writes the audit row in the same transaction.
3. The policy is an allowlist: unknown actions are denied. Failed claim check, injection flag, discount over the maximum,
   suppression and the kill switch are hard blocks that never send.
4. Before any retry the outbox asks the provider first (message id, CRM record, envelope id).
5. Segments are config packs under `packs/` (`pack`, `enrichment`, `icp`, `messaging`, `policy`, `rate_card`,
   `case_studies`, `stages` and templates). No Python file names a segment, and a test enforces it. Two packs ship:
   `franchise_brands` and `agencies`, the second added by configuration only.
6. Lessons from calls and replies shape the *angle* of a draft (which approved message leads), never its facts.

## Quick start

```
make setup                  # venv, dependencies, a local settings file from the example
docker compose up -d --wait db   # Postgres on host port 5434
make verify                 # ruff + the offline test suite (no keys, no network)
.venv/bin/revos demo        # one fictional lead, arrival to handoff, offline and narrated
```

API port: 8100. Postgres host port: 5434 (`POSTGRES_PORT`).

## Running it

```
.venv/bin/revos doctor                    # per service: REAL OK / REAL FAILED / MOCK
.venv/bin/revos run                       # API + worker + scheduler in one process; Ctrl-C stops cleanly
.venv/bin/revos seed acme --profile hot   # one fictional lead at <your prospect inbox>+acme@..., queued
.venv/bin/revos trace --lead name+acme@gmail.com   # that deal's timeline (add --json)
.venv/bin/revos trace --recent            # recent audit entries and held replies
.venv/bin/revos logs --deal <deal id>     # structured logs from var/logs/revos.jsonl
.venv/bin/revos linkedin draft <deal id>  # a LinkedIn note + message to copy; sends nothing
.venv/bin/revos kill-switch on|off        # pause or resume all outbound sending
.venv/bin/revos packs                     # validate every segment pack
```

Settings live in a local, git-ignored settings file; never paste its values anywhere. Other commands: `io-terms`,
`io`, `reprocess-reply`, `calls list|assign|reassign|reset-match`, `queue dead-letters|retry|retry-held`,
`hubspot-setup|owners|backfill`, `reset-demo --hubspot`, `dashboard-seed`, `slack-test`.

### Slack buttons and webhooks need a tunnel

```
cloudflared tunnel --url http://localhost:8100 --no-autoupdate    # prints https://<your-tunnel>.trycloudflare.com
```

Set `PUBLIC_BASE_URL` to that URL and restart `revos run` (a quick tunnel gets a new URL every time). Then paste:

- Slack, Interactivity Request URL: `<PUBLIC_BASE_URL>/slack/interactions`
- Dropbox Sign callback: `<PUBLIC_BASE_URL>/dropboxsign/webhook`

Only those two webhooks and `/healthz` are reachable through the tunnel.

### Dashboard

Open `http://127.0.0.1:8100/dashboard/` on the machine running the app and sign in with `DASHBOARD_TOKEN`. It shows
the pipeline, each deal's journey, plain-English activity, pending vetoes and approvals, the queue and dead letters,
lessons, integrations and the controls (kill switch, autonomy mode, veto seconds). Every dashboard route is refused
through the public tunnel.

## Safety rules for real-world actions

- Outbound email is allowlisted to the owner's own addresses: `PROSPECT_TEST_EMAIL` (plus-addresses of exactly its base
  part) and the sender. Every email carries an unsubscribe line and a postal-address line.
- A veto window, daily and per-domain caps, send windows and a kill switch sit in front of every send.
- An LLM spend cap is enforced in code.
- Secrets are read from the settings file only and are redacted from logs.

## Tests

`make verify` runs ruff and the whole suite offline against a real Postgres (fake LLM, mock adapters). Live checks are
marked `live`, skipped by default and never part of `make verify`: `make live-gmail`, `make live-hubspot`,
`make live-llm`, `scripts/gmail_reply_check.py`.

## Credits

Ideas only, no code copied: Scout (`gelevanog/ai-sdr-agent`, MIT) for claim and injection checks, Agent-SDR
(`JamesUgbanu/agent-sdr`, MIT) for the agent loop, tool scoping and lessons. An n8n SDR workflow without a licence
informed ideas only. The earlier `agentic-revenue-system` repo is not reused.

See `PROJECT_BRIEF.md`, `PLAN.md`, `CLAUDE.md` and `docs/INTEGRATIONS.md` for the detail.
