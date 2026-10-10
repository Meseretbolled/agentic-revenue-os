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
