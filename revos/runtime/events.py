"""Event store. An event is stored under a unique idempotency key before any work runs."""

from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from revos.obs import current_correlation, log, new_correlation_id
from revos.ports.clock import Clock


def record_event(
    conn: psycopg.Connection,
    key: str,
    type_: str,
    payload: dict[str, Any],
    clock: Clock,
    *,
    run_at: datetime | None = None,
    max_attempts: int = 5,
) -> int | None:
    """Store the event and enqueue its job in one transaction.

    Returns the event id, or None if the key was seen before (a duplicate is a no-op).
    `run_at` in the future makes the job a timer.
    """
    now = clock.now()
    cid = current_correlation() or new_correlation_id()      # a new chain unless one is running
    with conn.transaction():
        row = conn.execute(
            "INSERT INTO events (idempotency_key, type, payload, received_at, correlation_id) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
            (key, type_, Jsonb(payload), now, cid),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "INSERT INTO jobs (event_id, type, payload, run_at, max_attempts, created_at, correlation_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (row[0], type_, Jsonb(payload), run_at or now, max_attempts, now, cid),
        )
    log("events", f"event recorded: {type_}", deal_id=payload.get("deal_id"), correlation_id=cid,
        event_id=row[0], key=key)
    return row[0]
