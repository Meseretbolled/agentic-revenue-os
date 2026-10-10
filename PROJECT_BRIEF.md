# Agentic Revenue OS: Project Brief

Read this file fully before writing any code. It explains what is being built, why, what has already been decided, what the owner has already set up, and what the research found. `PLAN.md` says what to build in which order. `CLAUDE.md` holds the standing rules.

Written on 2026-10-10 (Saturday evening, Addis Ababa time). Everything marked **UNVERIFIED** was not confirmed against a real service and must be tested.

---

## 1. What this project is, in one paragraph

An AI agent system that runs the revenue lifecycle of an ad-tech company end to end, with almost no human involvement: **prospect → enrich → score → outreach → reply handling → meeting → qualify → proposal/IO → e-signature → handoff**. HubSpot is the system of record. Agents run on the Claude Agent SDK (Python). The system sends real emails, posts to Slack, reads call transcripts from Fireflies, generates contracts and gets them signed through an e-signature service. It replaces the manual CRM work salespeople hate. It is being built for an AI Engineer interview at **The COOL Company** (product: **Cool AI**), and the demo is a **recorded video**, so everything must visibly work end to end, like a production release, not a slide deck.

Repo name: **`agentic-revenue-os`** (new, empty repo). An older repo called `agentic-revenue-system` exists in the owner's GitHub. **Do not use, copy from or extend it.** The owner decided to start fresh.

---

## 2. The interview task (original brief, summarised)

**Context.** The COOL Company is an AI-powered advertising platform (Cool AI) with three stages: *Create* (creative automation, dynamic creative optimisation), *Activate* (media activation, franchise and location, publisher experiences, a supply-side platform), *Optimize* (measurement, attribution, insights, reporting). Customer segments: **brands (including franchise/multi-location), agencies, publishers, demand partners**. Each segment moves leads from first outreach to a signed IO (insertion order) or contract. Today much of that is manual.

**Objective.** Design an agent system that automates the lifecycle, then build and demo a working vertical slice.

**Requirements the design must cover**

1. **Agents.** Single-responsibility agents coordinated by an orchestrator that owns deal state.
2. **Skills.** Reusable core skills plus a per-segment config pack (ICP, messaging, scoring weights, templates). Adding a segment must not require agent code changes.
3. **Triggers.** Event-driven workflows (webhooks, schedules, SLA timers) that are durable and idempotent.
4. **Lead scoring.** Fit, intent and engagement sub-scores, a tier, and a human-readable reason for each score.
5. **Autonomy policy.** Low-risk actions auto-execute; high-risk actions wait for human approval (first touch to top-tier leads, pricing, IOs/contracts).
6. **Knowledge loop.** Call transcripts (Fireflies) and replies become structured insights and lessons that feed later outreach and call briefs across all segments.
7. **Tracking.** Stage exit criteria, reminders, stall detection, flags, digests.
8. **IO/contract automation.** Generate the document from deal data, validate it, route for approval, send for e-signature, close the deal when signed.
9. **Guardrails.** Scoped tools per agent, inbound content treated as untrusted data, an audit log, a kill switch.

**Constraints.** System of record HubSpot (mocking allowed). Agent runtime: Claude Agent SDK. Language: Python, modular, SOLID. Mock data allowed (sample leads, emails, transcripts, an IO template).

**Deliverables.** (1) Design doc, max 4 pages: architecture diagram, each agent's responsibilities and tools, skills and segment packs, trigger map, data model, autonomy policy, phased rollout. (2) Working vertical slice for one segment showing: a new lead enriched, scored and tiered; outreach drafted with high-risk sends going to an approval queue; a reply classified and flagged; a call transcript digested into deal fields and a lesson; an IO draft generated from deal data. (3) Second segment demo by adding **only a config pack, no agent code changes**. (4) Repo with README, setup steps and tests for the core skills.

**Demo (30 minutes in the original brief).** 10 min architecture and trade-offs, 15 min live run, 5 min Q&A (scaling, failure modes, evaluation). The owner will instead **record a video** showing the system working end to end, with a short architecture explanation.

**Evaluation.** Architecture (clear agent boundaries, orchestration, durability). Extensibility (new segment via config only). Safety (autonomy policy enforced in code, not prompts). Learning loop (lessons captured, retrieved and visibly used). Code quality (modular, SOLID, tested, readable). Communication (clear doc and demo, honest about trade-offs).

---

## 3. What the owner added beyond the brief

The owner has had interview rounds with the company and relayed these points:

- The interviewer said the owner is strong on technical understanding but wants to **see it as a production system**: agents visibly working, real automation, real integrations (HubSpot, email, Slack; social media where possible), not only mocks.
- **Very little human involvement.** The interviewer pointed out that waiting for a human to reply is the weakness of approval-heavy designs.
- **Case studies sent automatically** to customers who need proof to decide.
- Use **tools, desktop and MCP** where it makes sense, and "get something interesting".
- Think about **the real pain of salespeople** and remove it (see section 8).
- Align with **how The COOL Company actually works** (section 7).
- Use mock data where needed, but try to use real services.
- The owner has about 1.5 to 2 days. The video is due **Monday night (2 to 4 hours before midnight) or Tuesday morning**.

The owner explicitly decided: **no step waits for a human to answer** ("forget the brief document" on this point). See the autonomy policy in section 6.

---

## 4. What has been set up already (accounts and environment)

Secrets live only in the owner's local `.env` (never in chat, never in git). Variable names are in `.env.example`.

| Service | Status | Env variable(s) | Notes |
|---|---|---|---|
| HubSpot | Done. A fresh developer/test account, **EU region** (token starts `pat-eu1`). A **service key** was created (private apps are no longer offered). 12 scopes (CRM object scopes plus schema scopes; the exact list is in the HubSpot service key settings, check it if a call returns 403) | `HUBSPOT_TOKEN`, `HUBSPOT_BASE_URL` | Base URL must be configurable. **UNVERIFIED:** which base URL the EU portal needs. Try `https://api.hubapi.com` first, then `https://api-eu1.hubapi.com`, and keep whichever answers. **UNVERIFIED:** whether the service key works as a normal Bearer token. Test first with `GET /crm/v3/objects/contacts?limit=1`. |
| Slack | App "Revenue System" created in a free workspace, bot added to channel `#revops`. Bot token saved. | `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_CHANNEL` | Signing secret (Basic Information → App Credentials) was **not confirmed saved**. Interactivity (Cancel buttons) needs a public URL via the tunnel: set the Request URL later. |
| Gmail (sender) | Done. 2-Step Verification on; app password `revenue-system` created. | `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` | SMTP (send) and IMAP (read replies). |
| Gmail (prospect) | Done. A second, separate inbox plays the customer. A "never send to Spam" filter was created there. | `PROSPECT_TEST_EMAIL` | The owner replies from it by hand on camera. |
| Fireflies | Done. Free plan, no card. Auto-join and recap-sharing were restricted so it does not touch real meetings. API key saved. | `FIREFLIES_API_KEY` | Free plan: 3 transcriptions. Record one good mock sales call, keep two for retries. API limits seen: 30 requests/minute, 500/day. Fireflies also exposes an MCP URL, `https://api.fireflies.ai/mcp` (OAuth). |
| Dropbox Sign | Done. Free account; API key created. | `DROPBOX_SIGN_API_KEY` | Keep `test_mode`. **UNVERIFIED:** free-account API limits and whether test-mode requests work; fallback is a built-in one-click signing page that still triggers close. |
| Tavily | Key obtained (the site first blocked the owner with a CloudFront 403, then worked). | `TAVILY_API_KEY` | Optional. **UNVERIFIED** that the key works; test with one search. Without it, use mock research or fetch the company's own site. |
| Anthropic API (Console) | **Account created, no credit yet.** Billing is blocked on card details. | `ANTHROPIC_API_KEY` | The only paid item. Credits $5 to $10 were considered enough. Until a key exists, build and test with the offline fake LLM. A `$` spend cap must exist in code. The Claude Agent SDK may instead use the owner's existing Claude login when no key is set (**UNVERIFIED**; a key, if set, takes precedence). |
| Cloudflare tunnel | `cloudflared` not installed yet (a 18 MB `.deb` download was in progress, slow). | n/a | Needed so Slack, Fireflies and Dropbox Sign can reach the laptop. Install: `sudo dpkg -i cloudflared-linux-amd64.deb`, or Cloudflare's apt repo. |

**The owner's machine:** Ubuntu Linux (HP Pavilion), Python 3.12.3, git 2.43, Docker engine (Docker context set to `default`; **not** Docker Desktop), Docker Compose v2.39. **Other containers already run on ports 8000 and 8012. Do not touch them.** This project uses **app port 8100** and **Postgres port 5434**.

**The owner works in VS Code with the Claude Code extension** (Claude Pro login, so usage limits apply: prefer Sonnet, keep tasks small, commit often).

---

## 5. Research: public repos reviewed (what to borrow, what breaks)

Findings come from reading READMEs and key source files on 2026-10-10. Credit sources openly in the README. **Build your own implementation; copy no code.**

| Repo | License | Stack | What it covers | Worth borrowing | Where it breaks or is missing |
|---|---|---|---|---|---|
| **Scout**: https://github.com/gelevanog/ai-sdr-agent | MIT | Python 3.12, FastAPI, Postgres 17, Docker Compose | Account research with cited facts, rules-first ICP qualification (LLM may adjust by at most ±10), drafts with sourced claims, approval before send, reply classification, suppression, caps, CRM sync, audit log, evaluation suites | Rules-first scoring with bounded LLM nudge. Every claim must quote text found on its cited page (claim checker). Prompt-injection guard on crawled text. DB `CHECK` that blocks "sent" without an approver. Offline fake LLM and a 60-company synthetic test set with a rules-vs-LLM comparison. | Job queue has no retries, dead letters or idempotency keys, so a crash after sending can resend. The worker tick starves when jobs keep arriving. Send gate trusts the approval flag passed by the caller. Cap checks can race. HubSpot adapter never ran against a real portal. Sending is capture-only. One ICP file, no segment packs. |
| **Agent-SDR**: https://github.com/JamesUgbanu/agent-sdr | MIT | TypeScript, Next.js, Prisma, Postgres 16, Redis | Prospect research, outreach drafting, sequences, reply handling, meeting booking, HubSpot/Salesforce sync, approval queue (manual/assisted/autonomous), audit log | State machine where every status change goes through one function and writes the activity log in the same transaction. Tool calls blocked unless the record belongs to the run's workspace. Queue with exponential backoff, jitter, permanent-error detection, admin dead-letter retry. Agent loop with step caps, budgets, repeated-call blocking, resume, and a rule-based fallback when the LLM is down. | A crash between writing a step and recording its tool call can re-run a side effect on resume. Scope check passes everything when the workspace cannot be resolved (fail-open). Idempotency keys live in memory and reset on restart. Retries use in-process timers lost on restart. Policy check is a denylist, so unknown statuses pass. Scoring gives a number with no tier or written reason. Flow stops at meeting booked. No Slack, transcripts or e-signature. |
| **Autonomous AI SDR (n8n)**: https://github.com/asadxagentic-ai/autonomous-ai-sdr | **Not stated: ideas only, copy nothing** | n8n workflow export | Apollo → HubSpot contacts, ICP score, Gmail outreach, reply classifier (INTERESTED / NOT_INTERESTED / QUESTION / OOO), calendar booking, Slack alert | Simple reply routing. Plain HubSpot custom properties (outreach status, ICP score, sequence day). Cadence: intro, follow-up after 3 days, last note 4 days later. | Sends the email before updating HubSpot, so a failed update re-emails the lead. No pagination past 50 contacts. Out-of-office replies stall the lead forever. Replies checked once a day. Hard-coded sign-off. No tests, retries or approvals. |
| Awesome sales automation skills: https://github.com/sujayjayjay/awesome-sales-automation-skills | varies | prompt packs | BANT/MEDDIC qualification prompts | Skim for qualification prompt ideas only | Not an end-to-end system |
| HubSpot MCP server (Glama listing): https://glama.ai/mcp/servers/@SanketSKasar/HubSpot-MCP-Server | varies | Node/Python MCP | Read, search, basic create/update for contacts, deals, tickets | Shape of an MCP tool surface for CRM | Mostly read oriented. A thin custom HubSpot client is safer for writes and custom properties. |

**Coverage conclusion.** All three main repos stop at "meeting booked". None has call-transcript intake, learning loop, case studies, IO generation, e-signature, kill switch, Slack approvals (except n8n alerts), or per-segment packs. That later half of the lifecycle is this project's differentiator. Rough coverage of the 18 lifecycle capabilities in the research spreadsheet: agent-sdr 56%, Scout 50%, n8n 42%, this system's target 100%.

**Gaps this project must fix** (carried into the design, section 6): double-send on crash (use an outbox with idempotency keys and a provider check), in-memory idempotency (store in Postgres), fail-open scoping (fail closed), denylist policy (allowlist), caller-trusted approval (re-read from DB), racy caps (atomic in DB), UTC-only send hours (use the prospect's time zone), send-before-CRM-update (update CRM through the same outbox and make it retry-safe).

---

## 6. Architecture (what to build)

**Rule for every layer: the model proposes, code decides.** Agents call tools and return structured proposals. A Python policy engine and a Postgres-backed state machine decide what actually happens. Safety is a property of code, never of a prompt.

### 6.1 Stack

Python 3.12. Postgres 16 in Docker (host port 5434). FastAPI for webhooks and a small **server-rendered** dashboard (no separate frontend build, to save a day). Claude Agent SDK (`claude-agent-sdk` Python) for the agents. SQLAlchemy 2 + Alembic or plain SQL migrations. pytest. ruff. Docker Compose for Postgres, the API and the worker. `cloudflared` tunnel for inbound webhooks. HubSpot, Gmail (SMTP/IMAP), Slack, Fireflies (GraphQL), Dropbox Sign adapters, each behind an interface with a mock implementation. Offline fake LLM so tests need no keys.

### 6.2 Layers and SOLID boundaries

```
revos/
  domain/        entities, enums, events, errors, UntrustedText (no I/O, no dependencies)
  ports/         interfaces: CRM, Mailer, Inbox, Chat, Transcripts, ESign, Enrichment, LLM, Clock, Repositories
  adapters/      real and mock implementations of each port
  skills/        reusable pure skills (scoring, drafting helpers, claim check, extraction, IO build/validate, tracking)
  segments/      pack loader + schema validation; packs live in /packs/<segment>/
  policy/        autonomy engine, risk rules, kill switch, send gate, caps, suppression
  runtime/       durable queue (workers), outbox, event store, timers, scheduler
  orchestrator/  deal state machine and stage exit criteria; the only writer of deal stage
  agents/        single-responsibility agents on the Claude Agent SDK, each with scoped MCP tools
  mcp/           in-process MCP servers (CRM read, lessons, case studies, research), per agent scope
  api/           FastAPI: webhooks (signature verified), dashboard, approvals/veto, health
  cli/           doctor, reset-demo, seed, run-slice, hubspot-setup, kill-switch
tests/ (unit, integration, e2e with mocks), evals/
```

Dependency direction: `domain` ← `ports` ← `skills`/`policy` ← `orchestrator`/`agents` ← `adapters`/`api`. Only the composition root wires concrete adapters.

### 6.3 Deal state

One function changes a deal's stage and writes the audit entry **in the same transaction**. Stages: `new → enriched → scored → outreach → replied → meeting → qualified → proposal → io_approval → out_for_signature → closed_won | closed_lost → handoff`, plus a reachable `do_not_contact`. A stage cannot be entered unless its **exit criteria** (defined in the segment pack) pass. Stage moves come from what a tool recorded, never from what a model said.

### 6.4 Durability and idempotency

- **Events** are stored under a unique idempotency key before any work happens (webhooks, schedules, timers). A duplicate is a no-op.
- **Queue:** a Postgres table claimed with `FOR UPDATE SKIP LOCKED`; `run_at` column for timers; attempts counter with exponential backoff and jitter; permanent errors skip retry; a **dead-letter table** with a retry endpoint; a **lease timeout** so a crashed worker's jobs are picked up again.
- **Outbox pattern** for every side effect (email, CRM write, Slack post, signature request): store an `actions` row with a unique idempotency key, perform it, mark done with the provider's id. On retry, **check the provider first** (message id, CRM record, envelope id) before performing again.
- **Webhook signatures** verified for Slack, Fireflies and Dropbox Sign.
- DB constraints: `CHECK` that an action cannot be `sent` without an approver/decision record; a trigger that blocks sending to a suppressed contact; unique keys on idempotency columns.

### 6.5 Autonomy policy (in code)

An allowlist policy engine: any action type not explicitly listed is not allowed to run on its own. Each action has an autonomy level set **per action in the segment pack**: `auto`, `auto_with_veto` (waits a short window, then acts, with a Cancel button in Slack), or `needs_approval`. A global setting `AUTONOMY_MODE=autonomous|approval` switches every `auto_with_veto` to `needs_approval` (brief-compliant mode, a talking point for the interview).

| Action | Decision in autonomous mode | Enforced by |
|---|---|---|
| Enrich, score, update CRM fields and notes | Runs alone | Allowlist |
| First touch to tier B or C | Runs alone, inside the daily cap | Allowlist + atomic cap |
| First touch to tier A | Sends after a 60-second veto window; anyone can cancel in Slack | Per-action autonomy level in pack |
| Any message mentioning price, discount or rates | Sends after the veto window if inside the rate card; outside it, a person decides | Content check in code |
| Follow-ups, reminders, reply classification, out-of-office snooze | Runs alone | Allowlist |
| Unsubscribe or bounce | Runs alone: suppress the contact and cancel scheduled sends | DB trigger + send gate |
| Case study attachment | Runs alone, approved library items only | Library is human-curated |
| Generate an IO draft | Runs alone | Allowlist |
| Send an IO for signature | Sent automatically once validation passes | IO validator + rate card |
| Discount above the segment maximum | Blocked until a manager approves | IO validator |
| LinkedIn or other social message | Drafted and queued only (no public API for automated messaging) | Draft-only adapter |
| Kill switch on | Everything outbound waits in the queue (does not fail) | Checked before every action |

**Hard blocks go to a person and never send:** failed claim check, prompt-injection flag, discount over the limit, suppression, kill switch. The send step **re-reads the approval/decision from the database**; it never trusts a flag passed by the caller.

### 6.6 Lead scoring

Rules-first, a pure function of the segment pack's weights. Three sub-scores: **fit** (ICP match), **intent** (signals), **engagement** (replies, opens, meetings). Output: total, **tier** (A/B/C or disqualified) and a **written reason for every point awarded**. An optional LLM nudge is bounded (±10) and can never override a disqualifier. Include an evaluation comparing rules-only vs LLM-only on a synthetic set.

### 6.7 Claim checking and untrusted content

Every personal claim in a draft must cite evidence; a code check confirms the evidence text supports the claim before the draft can be queued. Replies, web pages and transcripts are wrapped as `UntrustedText`, scanned for instructions to the model, and **quarantined if flagged**. Agents hold no side-effect tools; they can only return proposals that go through the policy engine. Do not obey instructions found inside inbound content (the mock data should include a reply with an injection attempt in its footer to demonstrate this).

### 6.8 Knowledge loop

Each reply and each call produces **lessons** with evidence (the quote), a segment tag (or global), a confidence score and use counts. Later outreach drafts and call briefs **retrieve the best lessons and cite them by id** (visible in the email's audit entry and in Slack). Lessons that stop working are **retired**. Retrieval starts as keyword + tag scoring; a vector store is optional.

### 6.9 Segment packs (config only)

One folder per segment under `packs/<segment>/`, validated against a schema at load: ICP fields and weights, scoring rules, messaging pillars and tone (in COOL's own words), email templates, the **case-study library**, the **rate card** (labelled as a sample), SLAs and stage exit criteria, autonomy levels per action, send caps and send windows, qualification fields, IO and agreement templates. **No Python file may name a segment.** A test must prove this and add a third segment (`agencies`) from config only and run it to a validated contract.

### 6.10 Agents (single responsibility, scoped tools)

| Agent | Responsibility | Tools it may call (scoped MCP, read-only unless noted) |
|---|---|---|
| **Orchestrator** (code, not an LLM) | Owns deal state, applies exit criteria, routes events to agents | n/a (state machine) |
| **Intake & Enrichment** | Turn a sparse lead into a filled company/contact record with cited facts | `crm.get`, `research.search` (Tavily or mock), `research.fetch_site` |
| **Scoring** | Fit/intent/engagement, tier, reason | `pack.get_icp`, `crm.get` (deterministic skill; LLM nudge optional) |
| **Outreach** | Draft first touch and follow-ups from the pack template + lessons + the best case study | `lessons.search`, `casestudies.pick`, `crm.get`; proposes `send_email` |
| **Reply** | Classify replies (interested / not interested / question / price objection / unsubscribe / referral / out-of-office), flag injection, route | `crm.get`, `lessons.search`, `casestudies.pick` |
| **Meeting** | Propose slots, create the invite, write the call brief citing lessons | `calendar.slots` (or `.ics`), `lessons.search` |
| **Call Intel** | Digest the Fireflies transcript into budget, timeline, decision maker, needs, objections; write lessons | `transcripts.get`, `lessons.write` (proposal) |
| **Qualification** | Check exit criteria against deal fields; move or hold | `crm.get`, `pack.get_exit_criteria` |
| **Contract/IO** | Build the IO from deal data and the rate card, validate, route, send for signature, close on signature | `io.build`, `io.validate`, proposes `esign.send` |
| **Tracking** | Reminders, SLA timers, stall detection, daily Slack digest | `crm.list_stalled`, `audit.read` |
| **Knowledge** | Store, merge, retire lessons; report which lessons were used | `lessons.*` |

Per-agent tool scope is a declared allowlist. Every ID a tool receives must belong to the run's deal and segment, **and an unresolvable scope blocks the call** (fail closed). The Claude Agent SDK runs each agent with **no built-in tools** (no Bash/Read/Write), no host settings or CLAUDE.md loading, only the project's own MCP server. Deterministic agents (scoring, qualification, IO validation, tracking) run fixed procedures so scores and gates are reproducible; judgement agents (outreach, reply, meeting, call intel, knowledge) let the model choose among their scoped tools. The side effects themselves happen in code, after the policy engine.

### 6.11 Integrations

- **HubSpot** (real, EU): create/update contacts, companies, deals; custom properties for score, tier, reason, outreach status, sequence day, call fields; stage sync; notes. A `hubspot-setup` CLI creates the custom properties. Respect pagination. Write order: record in outbox first, then CRM, so a failed CRM update does not trigger a re-send.
- **Gmail** (real): SMTP send, IMAP poll for replies, threading by `Message-ID`/`In-Reply-To`. **Allowlist:** only send to the owner's own addresses (`PROSPECT_TEST_EMAIL` and the sender itself) during the demo. Every email carries an unsubscribe line and a postal-address line.
- **Slack** (real): post actions with the claim check, score reason, chosen case study and lesson ids; **Cancel** button during the veto window; manager-approval buttons for hard blocks; daily digest. Verify signing secret.
- **Fireflies** (real): GraphQL API to fetch transcripts; webhook or poll for "transcription complete". An MCP route (`https://api.fireflies.ai/mcp`) is an option for the interview's MCP point.
- **Dropbox Sign** (test mode): send the IO for signature; webhook `signature_request_all_signed` → deal Closed Won → Slack handoff. Webhook callback needs the HMAC/event-hash check Dropbox documents. Fallback: a built-in one-click signing page.
- **Social (LinkedIn etc.):** drafts only, queued for a person. Say this openly: there is no public API for automated messages.
- **Calendar:** optional Google Calendar; otherwise email an `.ics` invite.
- **Research:** Tavily if the key works; otherwise mock research or fetch the company's own site.

### 6.12 Tracking

Stage exit criteria, SLA timers per stage, stall detection, reminders, flags and a daily digest to Slack listing what was sent, replied to, signed and stalled. A scheduler enqueues these as events with unique keys.

### 6.13 Guardrails

Scoped tools, untrusted-content handling, an append-only **audit log** (who/what/why/inputs/policy decision/lesson ids), a **kill switch** (setting checked before every outbound action; actions wait, not fail), spend cap on LLM usage, allowlisted recipients in demo mode.

### 6.14 Data model (tables, sketch)

`segments` (id, pack_version) · `companies` · `contacts` (suppressed flag) · `deals` (stage, version, segment, tier, scores, reason, hubspot ids, fields from call) · `events` (idempotency_key UNIQUE, type, payload, received_at, processed_at) · `jobs` (queue: run_at, attempts, lease_until, status, last_error) · `dead_letters` · `actions` (outbox: idempotency_key UNIQUE, type, risk, autonomy_level, status, decision_by, decided_at, provider_id, payload; `CHECK` sent requires a decision) · `approvals` / `vetoes` · `messages` (thread ids, direction) · `transcripts` · `lessons` (evidence, segment/global, confidence, uses, retired_at) · `lesson_uses` · `case_studies` (segment, tags, metric, source_url, label) · `case_study_sends` (deal, reply?) · `documents` (IO versions, validation result) · `audit_log` (append-only) · `settings` (kill switch, autonomy mode, caps).

### 6.15 Trigger map

| Trigger | Source | Event | Handler |
|---|---|---|---|
| New lead | HubSpot webhook or CLI/form | `lead.created` | Enrichment → Scoring |
| Lead scored | internal | `lead.scored` | Outreach (per tier/autonomy) |
| Veto window ends | timer (`run_at`) | `action.veto_expired` | Send gate |
| Reply arrives | IMAP poll (schedule) | `email.received` | Reply agent |
| Meeting booked | Reply/Meeting | `meeting.booked` | Call brief |
| Transcript ready | Fireflies webhook/poll | `transcript.ready` | Call Intel → Qualification |
| Qualified | Orchestrator | `deal.qualified` | Contract/IO |
| IO approved/validated | policy | `io.ready` | E-sign send |
| Signed | Dropbox Sign webhook | `io.signed` | Close won, handoff |
| Stall check | schedule | `tracking.stall_scan` | Tracking |
| Daily digest | schedule | `tracking.digest` | Slack |
| Slack button | Slack interactivity | `slack.action` | Cancel / approve |
| Kill switch toggled | CLI/dashboard | `system.kill_switch` | Policy engine |

### 6.16 Evaluation

Synthetic leads, replies and transcripts with known answers; an offline fake LLM; golden sets for scoring, reply classification and transcript extraction; rules-only vs LLM-only scoring comparison; report accuracy numbers in the README and design doc. State honestly what has and has not been run against real services.

---

## 7. The COOL Company alignment (use their own words)

From cool.co (public pages, read 2026-10-10): COOL sells **Cool AI** as one platform across **Create, Activate, Optimize**. COOL Media groups five products: **DCO and Creative Automation, Franchise and Multi-location, Media and Attribution, Publisher Experiences, Supply-Side Platform**.

| Segment | Product to pitch | Pain (from their site) | Case study (public testimonial) |
|---|---|---|---|
| Brands & franchise | Franchise and Multi-location, Creative Automation | Scaling local campaigns without losing brand consistency or compliance | Pearle Vision franchisee campaigns; Campanile localised hotel events; Renault dealership ads; Air France, 100+ markets in 25 languages |
| Brands, performance | Cool AI, Media Activation, Measurement & Attribution | Weak ROAS, unclear attribution | Global Beauty Brand: ROAS $15.52 vs $3.80, CPA $19.23 vs $87.64; Celebree: 17.5% more branded search; NHN: 50% uptick vs Google Ads |
| Agencies | Cool AI across Create, Activate, Optimize | Fragmented ad tech, manual reporting, limited capacity, proving ROI | Decathlon in-house ad server; Noisy Beast and Media47 dashboard testimonials |
| Publishers | Publisher Experiences, SSP | Low yield, weak engagement, complex programmatic sales | Evolve: engagement and monetisation up 11x vs Disqus; commenting unit loads 22% faster |

**Cautions (important, be honest in the demo):**
- These are **public customer testimonials on their website, not verified results.** Store every case study with its **source link** and the label "public testimonial".
- The Global Beauty Brand page states "339% ROAS increase" and "79% lower CPA", but its own dollar figures work out to about 308% and 78%. **Quote only the dollar figures.**
- COOL publishes **no rate card.** Ours is a **sample, labelled as one.**
- The demo leads are **fictional companies**, so no real brand or person is emailed.
- Sources: https://cool.co/ · https://cool.co/cool-ai/ · https://cool.co/agencies/ · https://cool.co/press/powerful-new-solution-for-advertisers/

---

## 8. Salesperson pains and how the system removes them (the interviewer asked for this)

| Pain | What the system does | Visible in the demo as |
|---|---|---|
| Typing data into the CRM | Enrichment and every email, reply and call write straight to HubSpot | A new lead appears fully filled in |
| Not knowing who to call first | Tier and a written reason on every lead | The reason text on the deal |
| Researching each prospect | Agent reads the company site and cites what it found | Claims in the email link to their source |
| Forgetting follow-ups | Durable timers send the next step and flag stalls | A stalled deal in the Slack digest |
| Slow reply handling | Replies read within minutes, classified, routed | A price objection answered with a case study |
| Customers asking for proof | Best-matching case study picked and attached | The email with the case study and its metric |
| Call notes never reaching the CRM | Transcript turned into deal fields and a lesson | HubSpot fields filling after the call |
| Relearning the same objections | Lessons cited in later emails and call briefs | The follow-up that cites the new lesson |
| Slow contracts | IO built, checked, approved and signed from deal data | Closed Won after the signature |
| Fear of AI mistakes | Policy, audit log and kill switch | The kill switch pausing the queue |

---

## 9. The video demo (target flow)

Total about 15 to 20 minutes of screen recording plus a short architecture intro.

1. **Architecture (about 4 min):** one diagram; "the model proposes, code decides"; Postgres as source of truth; what is real and what is mocked.
2. **Health check:** `revos doctor` shows every integration green (HubSpot, Slack, Gmail, Fireflies, Dropbox Sign, Postgres, LLM budget).
3. **A new fictional lead arrives** → appears in HubSpot enriched, scored, tiered with a written reason, no clicks.
4. **Outreach goes out automatically.** Slack shows the claim check, score reason, chosen case study, lessons used, and a **Cancel** button during the 60-second veto window. Show a hard block going to a person (e.g. a discount above the maximum or an injection attempt).
5. **The owner replies from the prospect inbox** with a price objection (with an injection attempt hidden in the footer). The system classifies, flags the injection, answers with the matching **case study**, all in HubSpot.
6. **Meeting booked** → call brief citing lessons.
7. **Fireflies transcript** → deal fields filled in HubSpot + a new lesson with evidence.
8. **Follow-up email cites the new lesson.**
9. **Qualification gate** → IO generated from deal data, validated, sent through Dropbox Sign (test mode) → signature → **Closed Won** → Slack handoff.
10. **Resilience:** replay the same webhook (no duplicate), flip the **kill switch** (queue waits), show the **audit log** and the **dead-letter** retry.
11. **Second segment** (agencies) added by dropping in a config pack only.
12. **Honest trade-offs:** autonomous vs brief-compliant approval mode (one setting), social is draft-only, e-sign in test mode, LLM behaviour varies, what was not run live.

---

## 10. Risks and honest limits

- **Live integrations are untested until the first real run.** Every adapter has a mock behind the same interface, so the demo still runs if a service fails.
- Fireflies free plan allows only 3 transcriptions: one good mock call plus two retries.
- Dropbox Sign test mode watermarks documents and is not legally binding. Say so.
- LinkedIn and other social networks have no public API for automated messaging: drafts only.
- The n8n repo has no license: ideas only.
- The Anthropic key may arrive late: build and test on the offline fake LLM first. Keep a spend cap in code.
- The owner's Claude Pro usage limit may throttle long sessions: small tasks, frequent commits, use Sonnet.
- Time: if Sunday slips, cut the dashboard to a Slack-only approval flow and keep the agencies pack for last. Steps 1 and 2 of the plan matter most for the demo.
- Do not email real strangers. Real outreach needs the right to contact them, an unsubscribe link and a postal address in every message; those are built in, but the demo uses only fictional leads and addresses the owner owns.

---

## 11. Deliverables checklist for the interview

- [ ] Working system run end to end against real accounts (and a mock-only mode)
- [ ] Segment 1 (franchise brands) full slice; segment 2 (agencies) via config only; a test proving no code names a segment
- [ ] README with setup steps, `.env.example`, `revos doctor`
- [ ] Tests for core skills, policy, state machine, queue, idempotency; evaluation numbers
- [ ] Design doc, **max 4 pages**: architecture diagram, agents and tools, skills and packs, trigger map, data model, autonomy policy, phased rollout
- [ ] Recorded demo video (due Monday night or Tuesday morning)
- [ ] Credit the public repos in the README

The research spreadsheets (`revenue-system-research.xlsx`, `esignature-options.xlsx`) and the strategy doc "Agentic Revenue System: Build Strategy" exist in the owner's Claude workspace and hold the same research in tabular form.
