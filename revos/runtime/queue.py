"""Postgres job queue: SKIP LOCKED claims, leases, backoff, dead letters."""

import random
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from revos.ports.clock import Clock

BACKOFF_BASE_SECONDS = 5.0
BACKOFF_CAP_SECONDS = 3600.0


@dataclass(frozen=True)
class Job:
    id: int
    type: str
    payload: dict[str, Any]
    attempts: int          # includes this run; also the fencing token for complete/fail
    max_attempts: int
    event_id: int | None
    correlation_id: str | None = None


def backoff_seconds(attempt: int, rng: random.Random) -> float:
    """Exponential backoff with equal jitter: between half and all of base * 2^(n-1)."""
    ceiling = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
    return ceiling / 2 + rng.uniform(0, ceiling / 2)


def _dead_letter(cur: psycopg.Cursor, job_id: int, error: str) -> None:
    cur.execute(
        "INSERT INTO dead_letters (job_id, type, payload, error) "
        "SELECT id, type, payload, %s FROM jobs WHERE id = %s",
        (error, job_id),
    )
    cur.execute(
        "UPDATE jobs SET status = 'dead', lease_until = NULL, last_error = %s WHERE id = %s",
        (error, job_id),
    )


def claim_job(conn: psycopg.Connection, clock: Clock, lease_seconds: float = 60) -> Job | None:
    """Claim one due job, or one whose worker's lease expired. Two callers never get the same."""
    now = clock.now()
    with conn.transaction():
        cur = conn.cursor(row_factory=dict_row)
        while True:
            row = cur.execute(
                "SELECT * FROM jobs "
                "WHERE (status = 'queued' AND run_at <= %(now)s) "
                "   OR (status = 'running' AND lease_until < %(now)s) "
                "ORDER BY run_at, id FOR UPDATE SKIP LOCKED LIMIT 1",
                {"now": now},
            ).fetchone()
            if row is None:
                return None
            if row["status"] == "running" and row["attempts"] >= row["max_attempts"]:
                _dead_letter(cur, row["id"], "lease expired on the last attempt (worker crashed?)")
                continue
            attempts = row["attempts"] + 1
            cur.execute(
                "UPDATE jobs SET status = 'running', attempts = %s, "
                "lease_until = %s + make_interval(secs => %s) WHERE id = %s",
                (attempts, now, lease_seconds, row["id"]),
            )
            return Job(row["id"], row["type"], row["payload"], attempts, row["max_attempts"],
                       row["event_id"], row.get("correlation_id"))


def complete_job(conn: psycopg.Connection, job: Job, clock: Clock) -> bool:
    """Mark done. False if the lease was lost and another worker owns the job now."""
    with conn.transaction():
        done = conn.execute(
            "UPDATE jobs SET status = 'done', lease_until = NULL "
            "WHERE id = %s AND status = 'running' AND attempts = %s",
            (job.id, job.attempts),
        ).rowcount
        if done and job.event_id:
            conn.execute("UPDATE events SET processed_at = %s WHERE id = %s",
                         (clock.now(), job.event_id))
        return bool(done)


def fail_job(
    conn: psycopg.Connection,
    job: Job,
    error: str,
    clock: Clock,
    rng: random.Random,
    *,
    permanent: bool = False,
) -> str:
    """Retry later with backoff, or dead-letter. Returns 'retry', 'dead' or 'lost'."""
    with conn.transaction():
        cur = conn.cursor(row_factory=dict_row)
        owned = cur.execute(
            "SELECT 1 AS ok FROM jobs WHERE id = %s AND status = 'running' AND attempts = %s "
            "FOR UPDATE",
            (job.id, job.attempts),
        ).fetchone()
        if owned is None:
            return "lost"
        if permanent or job.attempts >= job.max_attempts:
            _dead_letter(cur, job.id, error)
            return "dead"
        run_at = clock.now().timestamp() + backoff_seconds(job.attempts, rng)
        cur.execute(
            "UPDATE jobs SET status = 'queued', lease_until = NULL, last_error = %s, "
            "run_at = to_timestamp(%s) WHERE id = %s",
            (error, run_at, job.id),
        )
        return "retry"


def release_job(conn: psycopg.Connection, job: Job) -> bool:
    """Give a running job back to the queue untouched (a clean shutdown that could not wait for it).
    The attempt it used is returned, so being interrupted never counts against its retry budget.
    False if the lease had already moved on."""
    with conn.transaction():
        return bool(conn.execute(
            "UPDATE jobs SET status = 'queued', lease_until = NULL, attempts = attempts - 1 "
            "WHERE id = %s AND status = 'running' AND attempts = %s", (job.id, job.attempts)).rowcount)


def list_dead_letters(conn: psycopg.Connection) -> list[dict[str, Any]]:
    cur = conn.cursor(row_factory=dict_row)
    return cur.execute(
        "SELECT id, job_id, type, error, created_at, retried_at FROM dead_letters ORDER BY id"
    ).fetchall()


def retry_dead_letter(conn: psycopg.Connection, dead_letter_id: int, clock: Clock,
                      actor: str = "operator") -> int:
    """Put a dead-lettered job back on the queue with a fresh attempt budget."""
    with conn.transaction():
        cur = conn.cursor(row_factory=dict_row)
        dl = cur.execute(
            "SELECT * FROM dead_letters WHERE id = %s FOR UPDATE", (dead_letter_id,)
        ).fetchone()
        if dl is None:
            raise LookupError(f"no dead letter {dead_letter_id}")
        if dl["retried_at"] is not None:
            raise ValueError(f"dead letter {dead_letter_id} was already retried")
        now = clock.now()
        cur.execute(
            "UPDATE jobs SET status = 'queued', attempts = 0, run_at = %s, lease_until = NULL, "
            "last_error = NULL WHERE id = %s",
            (now, dl["job_id"]),
        )
        cur.execute("UPDATE dead_letters SET retried_at = %s WHERE id = %s", (now, dead_letter_id))
        cur.execute(
            "INSERT INTO audit_log (actor, event, detail) VALUES (%s, 'job.retried', %s)",
            (actor, Jsonb({"dead_letter_id": dead_letter_id, "job_id": dl["job_id"]}),),
        )
        return dl["job_id"]
