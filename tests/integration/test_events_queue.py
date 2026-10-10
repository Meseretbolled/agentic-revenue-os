import random
import threading
from datetime import timedelta

import psycopg
import pytest

from revos.domain.errors import PermanentError
from revos.runtime.events import record_event
from revos.runtime.queue import (
    BACKOFF_BASE_SECONDS,
    backoff_seconds,
    claim_job,
    list_dead_letters,
    retry_dead_letter,
)
from revos.runtime.worker import Worker


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def job_row(conn, job_id: int) -> dict:
    cols = ["status", "attempts", "run_at", "last_error"]
    row = conn.execute(f"SELECT {', '.join(cols)} FROM jobs WHERE id = %s", (job_id,)).fetchone()
    return dict(zip(cols, row, strict=True))


# 1. duplicate events ---------------------------------------------------------------

def test_duplicate_event_is_a_noop(conn, clock) -> None:
    first = record_event(conn, "webhook:abc", "lead.created", {"n": 1}, clock)
    second = record_event(conn, "webhook:abc", "lead.created", {"n": 2}, clock)
    assert first is not None and second is None
    assert count(conn, "events") == 1
    assert count(conn, "jobs") == 1
    assert conn.execute("SELECT payload FROM events").fetchone()[0] == {"n": 1}


def test_duplicate_events_racing_create_one_job(conn, conn2, clock) -> None:
    results = []
    def fire(c):
        results.append(record_event(c, "race", "lead.created", {}, clock))
    threads = [threading.Thread(target=fire, args=(c,)) for c in (conn, conn2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(r is None for r in results) == [False, True]
    assert count(conn, "jobs") == 1


# 2. SKIP LOCKED ----------------------------------------------------------------------

def test_locked_job_is_skipped_by_another_claimer(conn, conn2, clock) -> None:
    for i in range(2):
        record_event(conn, f"e{i}", "t", {"i": i}, clock)
    with conn.transaction():                       # worker A holds its claim open
        a = claim_job(conn, clock)
        b = claim_job(conn2, clock)                # worker B must skip A's locked row
    assert a is not None and b is not None and a.id != b.id


def test_concurrent_workers_never_share_a_job(test_db_url, conn, clock) -> None:
    for i in range(30):
        record_event(conn, f"e{i}", "t", {}, clock)
    claimed: list[list[int]] = [[], []]

    def work(slot: int) -> None:
        with psycopg.connect(test_db_url, autocommit=True) as c:
            while (job := claim_job(c, clock)) is not None:
                claimed[slot].append(job.id)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    all_ids = claimed[0] + claimed[1]
    assert len(all_ids) == 30 and len(set(all_ids)) == 30


# 3. lease expiry -------------------------------------------------------------------------

def test_job_is_reclaimed_after_the_lease_expires(conn, conn2, clock) -> None:
    record_event(conn, "e", "t", {}, clock)
    crashed = claim_job(conn, clock, lease_seconds=30)       # worker claims, then "dies"
    assert crashed is not None
    clock.advance(29)
    assert claim_job(conn2, clock) is None                   # lease still valid
    clock.advance(2)
    again = claim_job(conn2, clock)
    assert again is not None and again.id == crashed.id and again.attempts == 2


def test_late_result_from_a_crashed_worker_is_ignored(conn, conn2, clock) -> None:
    from revos.runtime.queue import complete_job
    record_event(conn, "e", "t", {}, clock)
    old = claim_job(conn, clock, lease_seconds=30)
    clock.advance(31)
    new = claim_job(conn2, clock)
    assert complete_job(conn, old, clock) is False           # zombie worker wakes up
    assert complete_job(conn2, new, clock) is True


def test_job_whose_worker_keeps_crashing_is_dead_lettered(conn, clock) -> None:
    record_event(conn, "e", "t", {}, clock, max_attempts=2)
    for _ in range(2):
        assert claim_job(conn, clock, lease_seconds=10) is not None
        clock.advance(11)
    assert claim_job(conn, clock) is None
    assert count(conn, "dead_letters") == 1


# 5. backoff, permanent errors, dead letters ------------------------------------------

def test_backoff_grows_exponentially_with_jitter() -> None:
    rng = random.Random(7)
    for attempt in range(1, 8):
        ceiling = BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
        samples = [backoff_seconds(attempt, rng) for _ in range(50)]
        assert all(ceiling / 2 <= s <= ceiling for s in samples)
        assert len(set(samples)) > 1                         # jitter, not a fixed number


def test_failed_job_is_retried_later_with_backoff(conn, clock) -> None:
    record_event(conn, "e", "boom", {}, clock)
    calls = []
    def flaky(c, job):
        calls.append(job.attempts)
        if job.attempts < 3:
            raise ConnectionError("provider down")
    worker = Worker({"boom": flaky}, clock, rng=random.Random(1))
    delays = []
    for _ in range(3):
        assert worker.run_once(conn)
        row = job_row(conn, 1)
        delays.append((row["run_at"] - clock.now()).total_seconds())
        assert not worker.run_once(conn)                     # not due yet
        clock.advance(max(delays[-1], 0) + 1)
    assert calls == [1, 2, 3]
    assert job_row(conn, 1)["status"] == "done"
    assert 2.5 <= delays[0] <= 5 and 5 <= delays[1] <= 10   # attempt 1 then 2


def test_permanent_error_skips_retry_and_is_dead_lettered(conn, clock) -> None:
    record_event(conn, "e", "bad", {}, clock)
    def bad(c, job):
        raise PermanentError("contact has no email")
    Worker({"bad": bad}, clock).run_once(conn)
    assert job_row(conn, 1)["status"] == "dead"
    assert job_row(conn, 1)["attempts"] == 1
    (dl,) = list_dead_letters(conn)
    assert dl["error"] == "contact has no email"


def test_retries_exhaust_into_the_dead_letter_table(conn, clock) -> None:
    record_event(conn, "e", "bad", {}, clock, max_attempts=2)
    def bad(c, job):
        raise RuntimeError("still broken")
    worker = Worker({"bad": bad}, clock, rng=random.Random(2))
    worker.run_once(conn)
    clock.advance(3600)
    worker.run_once(conn)
    assert job_row(conn, 1)["status"] == "dead"
    assert count(conn, "dead_letters") == 1


def test_unknown_job_type_is_dead_lettered(conn, clock) -> None:
    record_event(conn, "e", "nobody.handles.this", {}, clock)
    Worker({}, clock).run_once(conn)
    assert count(conn, "dead_letters") == 1


# 6. dead-letter retry ------------------------------------------------------------------

def test_dead_letter_can_be_retried_and_then_succeeds(conn, clock) -> None:
    record_event(conn, "e", "t", {}, clock)
    state = {"broken": True}
    def handler(c, job):
        if state["broken"]:
            raise PermanentError("fix pending")
    worker = Worker({"t": handler}, clock)
    worker.run_once(conn)
    (dl,) = list_dead_letters(conn)
    state["broken"] = False
    job_id = retry_dead_letter(conn, dl["id"], clock)
    assert worker.run_once(conn)
    assert job_row(conn, job_id)["status"] == "done"
    with pytest.raises(ValueError):
        retry_dead_letter(conn, dl["id"], clock)             # cannot retry twice


def test_cli_retry_command(conn, clock, test_db_url, monkeypatch) -> None:
    from typer.testing import CliRunner

    from revos.cli.main import app
    record_event(conn, "e", "bad", {}, clock)
    Worker({}, clock).run_once(conn)
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    runner = CliRunner()
    assert "bad" in runner.invoke(app, ["queue", "dead-letters"]).output
    result = runner.invoke(app, ["queue", "retry", "1"])
    assert result.exit_code == 0 and "re-queued" in result.output
    assert job_row(conn, 1)["status"] == "queued"
    assert runner.invoke(app, ["queue", "retry", "1"]).exit_code == 1


def test_clock_helper_sanity(clock) -> None:
    before = clock.now()
    clock.advance(60)
    assert clock.now() - before == timedelta(seconds=60)
