"""Recurring events. Each tick's key includes the time bucket, so ticking twice in the same
bucket (two schedulers, a restart) cannot create a duplicate."""

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
    Schedule("tracking.stall_scan", 3600),
    Schedule("tracking.digest", 86400),
    Schedule("lessons.sweep", 86400),          # silence after a lesson was used counts against it
    Schedule("queue.release_held", 300),      # safety net: sends stranded by the kill switch
)


def tick(
    conn: psycopg.Connection,
    clock: Clock,
    schedules: tuple[Schedule, ...] = DEFAULT_SCHEDULES,
) -> list[str]:
    """Enqueue the current bucket of each schedule. Returns the keys that were new."""
    created = []
    now = clock.now()
    for sched in schedules:
        bucket = int(now.timestamp() // sched.every_seconds)
        key = f"schedule:{sched.event_type}:{bucket}"
        if record_event(conn, key, sched.event_type, {"bucket": bucket}, clock) is not None:
            created.append(key)
    return created
