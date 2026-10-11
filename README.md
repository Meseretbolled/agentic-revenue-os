# Agentic Revenue OS

AI agents that run the revenue lifecycle (prospect to handoff) for an ad-tech company, with
HubSpot as the system of record, built on the Claude Agent SDK. Work in progress: see
`PROJECT_BRIEF.md`, `PLAN.md` and `CLAUDE.md`.

## Quick start

```
make setup          # venv, dependencies, .env from .env.example
docker compose up -d db   # Postgres on host port 5434
make verify         # ruff + tests
```

API port: 8100. Postgres host port: 5434.

## Running it

```
.venv/bin/revos doctor        # per service: REAL OK / REAL FAILED / MOCK, and what it will run as
.venv/bin/revos run           # API (8100) + worker + scheduler in one process; Ctrl-C stops cleanly
.venv/bin/revos seed acme     # one fictional lead at <your prospect inbox>+acme@... , queued
.venv/bin/revos trace --lead name+acme@gmail.com   # that deal's timeline (add --json)
.venv/bin/revos logs --deal <deal id>                       # structured logs from var/logs/revos.jsonl
```

A real adapter is used whenever its keys are in `.env`; a mock only when they are missing. The
startup banner of `revos run` lists REAL or MOCK for every integration, and the app refuses to
start if a service that will run as REAL fails its check (it never swaps in a mock quietly).
Slack, Fireflies, Dropbox Sign and Tavily have no adapter yet, so they run as MOCK even with a key.
See `docs/INTEGRATIONS.md` for what has and has not been run against the real services.
