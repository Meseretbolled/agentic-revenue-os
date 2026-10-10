# Build Plan: agentic-revenue-os

Work in this order. Each step is small enough for **one Claude Code session**. After every step: run the tests, run `ruff`, commit with a clear message, and tick the box. Do not start a step until the previous one's "Done when" is true. Read `PROJECT_BRIEF.md` first.

Time frame: it is Saturday evening 2026-10-10. The recorded demo is due Monday night or Tuesday morning. Target pace: steps 0 to 5 tonight and Sunday morning, steps 6 to 12 Sunday, live testing Sunday evening, polish and recording Monday.

Rules that apply to every step: see `CLAUDE.md`. Offline fake LLM and mock adapters first; real adapters after. Never print or commit secrets.

---

## Phase A: Foundation (must be solid)

### Step 0. Repo scaffold
- `pyproject.toml` (Python 3.12, `claude-agent-sdk`, FastAPI, uvicorn, SQLAlchemy 2, psycopg, Alembic or SQL migrations, pydantic, PyYAML, Jinja2, httpx, typer, rich, pytest, ruff), folder layout from the brief section 6.2, `Makefile`, `docker-compose.yml` (Postgres 16 on host port **5434**, API on **8100**, worker), `.env.example`, `.gitignore` (ignores `.env`, `var/`, `.venv`), README stub, CI-less `make verify` (ruff + tests).
- **Done when:** `make setup`, `docker compose up -d db`, and `make test` pass on an empty test; `git status` shows no secrets.

### Step 1. Domain, schema and the state machine
- Domain entities and enums; SQL migrations for all tables in section 6.14 including the **`CHECK` (sent requires a decision)**, the **suppression trigger**, and unique idempotency keys.
- The single `transition_deal()` function: validates the allowed move, checks the segment's exit criteria, writes the audit row **in the same transaction**; the `do_not_contact` state is reachable.
- **Done when:** unit tests prove illegal transitions fail, audit is written atomically, and the DB refuses an unapproved `sent` and a send to a suppressed contact (tests hit real Postgres).

### Step 2. Durable runtime: events, queue, outbox, timers
- Event store with unique idempotency keys; Postgres queue with `SKIP LOCKED`, `run_at` timers, attempts, exponential backoff with jitter, permanent-error detection, dead-letter table + retry endpoint/CLI, lease timeout; outbox with provider-check-before-retry; a scheduler for recurring events.
- **Done when:** tests prove: duplicate webhook is a no-op; a worker killed mid-job is recovered after the lease; a retry after a simulated crash does **not** double-send (provider check); timers survive a restart; dead letters can be retried.

### Step 3. Policy engine, kill switch, caps, audit
- Allowlist autonomy engine with levels `auto`, `auto_with_veto`, `needs_approval`, plus `AUTONOMY_MODE=autonomous|approval`; hard blocks; **send gate that re-reads the decision from DB**; atomic daily/per-domain caps with defer (not drop); prospect-time-zone send windows; suppression; kill switch (actions wait); recipient allowlist for demo mode; append-only audit log.
- **Done when:** a table-driven test covers every row of the autonomy table in the brief; unknown action types are denied; flipping the mode changes outcomes with no code change; kill switch pauses and resumes without losing actions.

### Step 4. Segment packs and scoring
- Pack schema + loader (validated), the `franchise_brands` pack (ICP, weights, messaging in COOL's words, templates, case-study library with source links and "public testimonial" labels, **sample** rate card, SLAs, exit criteria, autonomy levels, caps, qualification fields, IO and agreement templates).
- Scoring skill: fit/intent/engagement, tier, written reason per point; bounded LLM nudge hook that cannot override disqualifiers.
- **Done when:** golden-set scoring tests pass; a test asserts **no Python file names a segment**; invalid packs fail loudly with clear messages.

### Step 5. Safety skills and the LLM layer
- `UntrustedText` wrapper and injection scanner (quarantine flagged content); claim checker (claim must quote supporting evidence); offline **fake LLM** with deterministic outputs for classification, extraction and drafting; the real LLM adapter (Claude Agent SDK / API) behind the same port with a **spend cap**.
- **Done when:** tests prove an injection in a reply is flagged and not obeyed; an unsupported claim blocks a draft; the whole suite runs offline with no key.

---

## Phase B: Agents and mock end-to-end

### Step 6. Mock adapters and the MCP tool layer
- Mock CRM, mailer/inbox, chat, transcripts, e-sign, enrichment; in-process MCP servers (CRM read, lessons, case studies, research) with per-agent scopes that **fail closed** when an ID does not belong to the run's deal and segment.
- **Done when:** tests show an agent cannot call a tool outside its scope or touch another deal.

### Step 7. Agents on the Claude Agent SDK
- Enrichment, Scoring, Outreach, Reply, Meeting, Call Intel, Qualification, Contract/IO, Tracking, Knowledge. SDK options: no built-in tools, no host settings, only the project's MCP server, `max_turns` and budget caps. Deterministic agents run fixed procedures. Side effects are requested as proposals and executed in code after the policy engine. Rule-based fallback when the LLM is unavailable.
- **Done when:** with the fake LLM, a fictional lead goes lead → enriched → scored → outreach proposed → policy decision, all through the orchestrator, with audit entries; a hidden instruction in a mock reply is quarantined.

### Step 8. The full mock slice and the lessons loop
- Reply handling and routing (interested, not interested, question, price objection, unsubscribe, referral, out-of-office); case-study picker; meeting + call brief; transcript → deal fields + lessons with evidence; lesson retrieval cited in later drafts; retirement of lessons that stop working; qualification gate; IO generation + validation (rate card, discount limit, required fields); signature event → closed won → handoff.
- **Done when:** `make demo` runs lead-to-handoff offline, prints each step, and a test asserts a later email cites the lesson created from the call.

---

## Phase C: Real integrations (one at a time; each has a mock fallback and a `doctor` check)

### Step 9. `revos doctor`, HubSpot, Gmail
- `doctor`: checks env vars (never prints values), Postgres, tunnel, LLM budget, and each service with a harmless read call.
- HubSpot adapter (EU base URL configurable, pagination, retries), `hubspot-setup` to create custom properties, contact/company/deal sync and notes. **First, verify the service key works as a Bearer token.**
- Gmail SMTP send + IMAP reply polling with threading, recipient allowlist, unsubscribe and postal-address lines.
- **Done when:** a fictional lead appears in the real HubSpot portal, a real email reaches the prospect inbox, a reply written by hand is read, classified and logged. Mock mode still passes all tests.

### Step 10. Slack and the veto window
- Post actions with claim check, score reason, case study, lesson ids; **Cancel** button during the 60-second window (Request URL = tunnel + `/slack/interactions`, signature verified); hard-block approval buttons; daily digest.
- **Done when:** cancelling in Slack stops a queued send; letting it expire sends exactly once; a replayed Slack payload does nothing.

### Step 11. Fireflies and call intel
- Fireflies GraphQL adapter (transcript fetch; webhook or poll for completion); optional MCP route; extraction into deal fields; lessons written with evidence.
- **Done when:** the owner's recorded mock call fills the HubSpot deal fields and creates a lesson used by the next email.

### Step 12. IO, Dropbox Sign, signature webhook
- Dropbox Sign adapter (test mode); webhook with signature/event-hash verification; deal moves to Closed Won on `signature_request_all_signed`; Slack handoff. If the account cannot use the API, switch to the built-in one-click signing page and say so.
- **Done when:** an IO is generated, validated, sent, signed and the deal closes in HubSpot; the same webhook delivered twice does nothing.

---

## Phase D: Visibility and extensibility

### Step 13. Dashboard and demo commands
- Server-rendered dashboard: deal list with tier and reason, live agent activity, pending vetoes/approvals, audit trail, queue and dead letters, kill-switch toggle, autonomy-mode toggle. CLI: `reset-demo`, `seed`, `run-slice`, `kill-switch on|off`.
- **Done when:** a first-time viewer can follow one lead across stages on screen; `reset-demo` returns everything to a clean state in under a minute.

### Step 14. Second segment by config only
- Add the `agencies` pack (and optionally `publishers`) with no Python changes.
- **Done when:** the extensibility test passes and the demo runs the second segment through to a validated IO.

### Step 15. Evaluation
- Golden sets (scoring, reply classes, transcript extraction); rules-only vs LLM-only comparison; print accuracy numbers.
- **Done when:** `make eval` prints numbers that go straight into the README and design doc.

---

## Phase E: Documents and recording

### Step 16. README, design doc, demo script
- README: setup (including Docker ports and tunnel), what is real vs mocked, how to run, credits to the public repos (Scout, Agent-SDR; the n8n repo for ideas only).
- Design doc (**max 4 pages**): architecture diagram, agents and tools, skills and packs, trigger map, data model, autonomy policy, phased rollout, honest trade-offs.
- Demo script following brief section 9.
- **Done when:** a stranger can clone, set `.env`, and run `revos doctor` then the slice from the README alone.

### Step 17. Rehearse and record
- Two timed rehearsals, a backup screen recording, `reset-demo` before the final take, then record. Record the **real** run, say plainly what is mock or test mode.

---

## If time runs short (cut in this order)

1. Dashboard polish → Slack-only approval view.
2. Vector search for lessons → keyword + tag.
3. Calendar integration → `.ics` invite.
4. Tavily → mock research.
5. Dropbox Sign live → built-in signing page.
6. Keep **never**: the policy engine, the outbox/idempotency, the kill switch, the audit log, the lessons loop, the second segment by config.
