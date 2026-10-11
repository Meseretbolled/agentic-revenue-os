"""Worker: claims jobs and runs the handler registered for the job's type."""

import random
import time
from collections.abc import Callable, Mapping

import psycopg

from revos.adapters.clock import SystemClock
from revos.db import connect, migrate
from revos.domain.errors import PermanentError
from revos.ports.clock import Clock
from revos.runtime.queue import Job, claim_job, complete_job, fail_job

Handler = Callable[[psycopg.Connection, Job], None]


class Worker:
    def __init__(
        self,
        handlers: Mapping[str, Handler],
        clock: Clock,
        *,
        lease_seconds: float = 60,
        rng: random.Random | None = None,
    ) -> None:
        self.handlers = handlers
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.rng = rng or random.Random()

    def run_once(self, conn: psycopg.Connection) -> bool:
        """Run at most one job. Returns False when nothing was due."""
        job = claim_job(conn, self.clock, self.lease_seconds)
        if job is None:
            return False
        try:
            handler = self.handlers.get(job.type)
            if handler is None:
                raise PermanentError(f"no handler registered for job type {job.type!r}")
            handler(conn, job)
        except PermanentError as exc:
            fail_job(conn, job, str(exc), self.clock, self.rng, permanent=True)
        except Exception as exc:  # noqa: BLE001 - any handler failure is retried with backoff
            fail_job(conn, job, f"{type(exc).__name__}: {exc}", self.clock, self.rng)
        else:
            complete_job(conn, job, self.clock)
        return True


def main() -> None:
    """The long-running worker: runs jobs and, every half minute, enqueues the scheduled events
    (reply polling, stall scans, the kill-switch safety net)."""
    from revos.composition import build_orchestrator
    from revos.db import connect_read_only
    from revos.env import load_env_file
    from revos.runtime.scheduler import tick

    load_env_file()
    clock = SystemClock()
    with connect() as conn, connect_read_only() as ro:
        migrate(conn)                      # idempotent: a new release may add tables
        conn.autocommit = True
        worker = Worker(build_orchestrator(conn, ro, clock).handlers(), clock)
        last_tick = 0.0
        while True:
            if time.monotonic() - last_tick >= 30:
                tick(conn, clock)
                last_tick = time.monotonic()
            if not worker.run_once(conn):
                time.sleep(1)


if __name__ == "__main__":
    main()
