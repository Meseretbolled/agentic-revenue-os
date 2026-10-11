"""`revos run`: the API, the worker and the scheduler in one process, with a clean shutdown.

Shutdown never loses a job. The worker finishes the job it is running (up to a grace period); a job
that cannot finish in time is released back to the queue with its attempt returned, and the
outbox's idempotency keys make running it again safe."""

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import psycopg

from revos.obs import log
from revos.ports.clock import Clock
from revos.runtime.queue import release_job
from revos.runtime.scheduler import tick
from revos.runtime.worker import Handler, Worker

HandlerFactory = Callable[[psycopg.Connection, psycopg.Connection, Clock], Mapping[str, Handler]]


@dataclass
class ShutdownReport:
    finished_cleanly: bool = True
    released_job_ids: list[int] = field(default_factory=list)


class Supervisor:
    def __init__(self, *, database_url: str, read_only_url: str | None = None, clock: Clock,
                 build_handlers: HandlerFactory, tick_seconds: float = 30.0, idle_seconds: float = 0.5,
                 grace_seconds: float = 15.0, lease_seconds: float = 60.0,
                 api_port: int | None = None, api_host: str = "127.0.0.1") -> None:
        self.url, self.ro_url, self.clock = database_url, read_only_url, clock
        self.build_handlers = build_handlers
        self.tick_seconds, self.idle_seconds = tick_seconds, idle_seconds
        self.grace_seconds, self.lease_seconds = grace_seconds, lease_seconds
        self.api_port, self.api_host = api_port, api_host
        self.stop_event = threading.Event()
        self.worker: Worker | None = None
        self.api_ready = threading.Event()
        self.errors: list[str] = []
        self._threads: list[threading.Thread] = []
        self._api: Any = None
        self._ready = threading.Event()

    # --- lifecycle -----------------------------------------------------------------------------------
    def start(self) -> None:
        self._spawn("worker", self._work)
        self._spawn("scheduler", self._schedule)
        if self.api_port:
            self._spawn("api", self._serve_api)
        if not self._ready.wait(timeout=30):
            raise RuntimeError("the worker did not start within 30 seconds")
        if self.errors:
            self.stop()
            raise RuntimeError("startup failed: " + "; ".join(self.errors))
        log("run", "supervisor started", api_port=self.api_port)

    def stop(self) -> ShutdownReport:
        """Stop taking new work, let the current job finish, release it if it cannot."""
        report = ShutdownReport()
        self.stop_event.set()
        if self._api is not None:
            self._api.should_exit = True
        for thread in self._threads:
            thread.join(timeout=self.grace_seconds)
        worker = self.worker
        stuck = worker.current if worker else None
        if stuck is not None:
            report.finished_cleanly = False
            with psycopg.connect(self.url, autocommit=True) as conn:
                if release_job(conn, stuck):
                    report.released_job_ids.append(stuck.id)
            log("run", "released a running job back to the queue", level="warning", job_id=stuck.id)
        log("run", "supervisor stopped", clean=report.finished_cleanly)
        return report

    def wait(self) -> None:
        self.stop_event.wait()

    # --- threads -------------------------------------------------------------------------------------
    def _spawn(self, name: str, target: Callable[[], None]) -> None:
        thread = threading.Thread(target=self._guard(name, target), name=f"revos-{name}", daemon=True)
        self._threads.append(thread)
        thread.start()

    def _guard(self, name: str, target: Callable[[], None]) -> Callable[[], None]:
        def run() -> None:
            try:
                target()
            except Exception as exc:  # noqa: BLE001 - reported, then the whole process stops
                self.errors.append(f"{name}: {type(exc).__name__}: {exc}")
                log("run", f"{name} crashed", level="error", error=f"{type(exc).__name__}: {exc}")
                self._ready.set()
                self.stop_event.set()
        return run

    def _work(self) -> None:
        with psycopg.connect(self.url, autocommit=True) as conn, \
                psycopg.connect(self.ro_url or self.url, autocommit=True,
                                options="-c default_transaction_read_only=on") as ro:
            self.worker = Worker(self.build_handlers(conn, ro, self.clock), self.clock,
                                 lease_seconds=self.lease_seconds)
            self._ready.set()
            while not self.stop_event.is_set():
                if not self.worker.run_once(conn):
                    self.stop_event.wait(self.idle_seconds)

    def _schedule(self) -> None:
        with psycopg.connect(self.url, autocommit=True) as conn:
            while not self.stop_event.is_set():
                tick(conn, self.clock)
                self.stop_event.wait(self.tick_seconds)

    def _serve_api(self) -> None:
        import uvicorn

        from revos.api.main import app
        config = uvicorn.Config(app, host=self.api_host, port=self.api_port, log_level="warning")
        self._api = uvicorn.Server(config)
        self._api.install_signal_handlers = lambda: None      # the CLI owns Ctrl-C
        self._api.run()
