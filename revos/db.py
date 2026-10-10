"""Database access: connection URL and the SQL migration runner."""

import os
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_MIGRATION_LOCK = 727_001  # advisory lock id so two processes never migrate at once


def database_url() -> str:
    """DATABASE_URL if set, otherwise built from POSTGRES_PORT (default 5434)."""
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    port = os.environ.get("POSTGRES_PORT", "5434")
    return f"postgresql://revos:revos@localhost:{port}/revos"


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or database_url())


def connect_read_only(url: str | None = None) -> psycopg.Connection:
    """A connection whose transactions are read-only (used by agent tools).

    This stops accidental writes. It is not a privilege boundary: a SELECT-only database role
    would be the stronger version.
    """
    return psycopg.connect(url or database_url(), autocommit=True,
                           options="-c default_transaction_read_only=on")


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply any unapplied migrations in filename order. Safe to re-run."""
    applied_now: list[str] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied_now.append(path.name)
    return applied_now
