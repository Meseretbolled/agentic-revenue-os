"""Recurring events. Each tick's key includes the time bucket, so ticking twice in the same
bucket (two schedulers, a restart) cannot create a duplicate."""

import os
from dataclasses import dataclass

import psycopg

from revos.ports.clock import Clock
from revos.runtime.events import record_event


@dataclass(frozen=True)
class Schedule:
    event_type: str
    every_seconds: int


DEFAULT_SCHEDULES: tuple[Schedule, ...] = (
    Schedule("email.poll", 60),
    Schedule("calls.poll", 300),               # finished Fireflies transcripts (the webhook is not used)
    Schedule("tracking.stall_scan", 3600),
    Schedule("tracking.digest", 86400),
    Schedule("lessons.sweep", 86400),          # silence after a lesson was used counts against it
    Schedule("queue.release_held", 300),      # safety net: sends stranded by the kill switch
)


def with_overrides(schedules: tuple[Schedule, ...]) -> tuple[Schedule, ...]:
    """CALLS_POLL_SECONDS shortens the Fireflies poll for a demo (never below 30 seconds)."""
    raw = os.environ.get("CALLS_POLL_SECONDS", "").strip()
    if not raw.isdigit():
        return schedules
    seconds = max(30, int(raw))
    return tuple(Schedule(s.event_type, seconds) if s.event_type == "calls.poll" else s for s in schedules)


def tick(
    conn: psycopg.Connection,
    clock: Clock,
    schedules: tuple[Schedule, ...] | None = None,
) -> list[str]:
    """Enqueue the current bucket of each schedule. Returns the keys that were new."""
    schedules = schedules if schedules is not None else with_overrides(DEFAULT_SCHEDULES)
    created = []
    now = clock.now()
    for sched in schedules:
        # A tick whose predecessor has not finished is skipped: polls are idempotent, and a slow poll must not
        # pile up a backlog that delays the sends queued behind it.
        if conn.execute("SELECT 1 FROM jobs WHERE type = %s AND status IN ('queued', 'running') LIMIT 1",
                        (sched.event_type,)).fetchone():
            continue
        bucket = int(now.timestamp() // sched.every_seconds)
        key = f"schedule:{sched.event_type}:{bucket}"
        if record_event(conn, key, sched.event_type, {"bucket": bucket}, clock) is not None:
            created.append(key)
    return created
