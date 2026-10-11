.PHONY: setup lint test verify db-up db-down demo live-hubspot live-gmail live-llm

VENV := .venv
PY := $(VENV)/bin/python

setup:
	test -d $(VENV) || python3.12 -m venv $(VENV)
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -e ".[dev]"
	test -f .env || cp .env.example .env

lint:
	$(PY) -m ruff check .

test: db-up
	$(PY) -m pytest -q

verify: lint test

# Port comes from POSTGRES_PORT (default 5434, set in docker-compose.yml and .env).
db-up:
	docker compose up -d --wait db

db-down:
	docker compose down

# One fictional lead from arrival to handoff, offline and narrated (uses its own database).
demo: db-up
	$(VENV)/bin/revos demo

# Real HubSpot, real credentials from .env. Creates and removes ONE fictional contact and deal.
# Not part of `make verify`.
live-llm: db-up
	$(PY) -m pytest -m live tests/live/test_llm_live.py -s -v

live-hubspot: db-up
	$(VENV)/bin/revos hubspot-setup
	$(PY) -m pytest -m live tests/live/test_hubspot_live.py -v

# Real Gmail, real credentials from .env. Sends ONE email to PROSPECT_TEST_EMAIL. Not part of `make verify`.
live-gmail:
	$(PY) -m pytest -m live tests/live/test_gmail_live.py -v
