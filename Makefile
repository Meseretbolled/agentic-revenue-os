.PHONY: setup lint test verify db-up db-down

VENV := .venv
PY := $(VENV)/bin/python

setup:
	test -d $(VENV) || python3.12 -m venv $(VENV)
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -e ".[dev]"
	test -f .env || cp .env.example .env

lint:
	$(PY) -m ruff check .

test:
	$(PY) -m pytest -q

verify: lint test

# Port comes from POSTGRES_PORT (default 5434, set in docker-compose.yml and .env).
db-up:
	docker compose up -d db

db-down:
	docker compose down
