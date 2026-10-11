import random

from revos.runtime.events import record_event
from revos.runtime.scheduler import DEFAULT_SCHEDULES, Schedule, tick
from revos.runtime.worker import Worker

# 7. timers ------------------------------------------------------------------------------

def test_timer_survives_a_restart_and_fires_once_at_the_right_time(conn, conn2, clock) -> None:
    fired = []
    handlers = {"action.veto_expired": lambda c, job: fired.append(clock.now())}
    due = clock.now().timestamp() + 60
    from datetime import UTC, datetime
    record_event(conn, "veto:action1", "action.veto_expired", {}, clock,
                 run_at=datetime.fromtimestamp(due, UTC))

    first_process = Worker(handlers, clock, rng=random.Random(1))
    clock.advance(59)
    assert not first_process.run_once(conn)                     # too early
    conn.close()                                                # the process dies

    restarted = Worker(handlers, clock, rng=random.Random(1))   # new process, new connection
    assert not restarted.run_once(conn2)                        # still 1 second early
    clock.advance(1)
    assert restarted.run_once(conn2)
    assert fired == [clock.now()]
    clock.advance(3600)
    assert not restarted.run_once(conn2)                        # fired exactly once
    assert len(fired) == 1


def test_timer_event_is_idempotent_by_key(conn, clock) -> None:
    from datetime import UTC, datetime
    when = datetime.fromtimestamp(clock.now().timestamp() + 60, UTC)
    assert record_event(conn, "veto:a1", "t", {}, clock, run_at=when) is not None
    assert record_event(conn, "veto:a1", "t", {}, clock, run_at=when) is None
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


# 8. scheduler -----------------------------------------------------------------------------

def test_scheduler_twice_in_the_same_bucket_creates_no_duplicates(conn, clock) -> None:
    first = tick(conn, clock)
    second = tick(conn, clock)
    assert len(first) == len(DEFAULT_SCHEDULES) and second == []
    keys = [r[0] for r in conn.execute("SELECT idempotency_key FROM events")]
    assert len(keys) == len(set(keys)) == len(DEFAULT_SCHEDULES)
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == len(DEFAULT_SCHEDULES)


def test_scheduler_adds_a_new_event_when_the_bucket_changes(conn, clock) -> None:
    schedules = (Schedule("email.poll", 60),)
    assert len(tick(conn, clock, schedules)) == 1
    conn.execute("UPDATE jobs SET status = 'done'")              # the first poll finished
    clock.advance(61)
    assert len(tick(conn, clock, schedules)) == 1
    clock.advance(5)
    assert tick(conn, clock, schedules) == []
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 2


def test_a_slow_poll_does_not_pile_up_a_backlog(conn, clock) -> None:
    polls = (Schedule("email.poll", 60),)
    assert tick(conn, clock, polls) != []
    for _ in range(5):                                           # five minutes pass, the first poll never ran
        clock.advance(60)
        assert tick(conn, clock, polls) == []
    assert conn.execute("SELECT count(*) FROM jobs WHERE type = 'email.poll'").fetchone()[0] == 1


def test_the_next_poll_is_scheduled_once_the_previous_finished(conn, clock) -> None:
    polls = (Schedule("email.poll", 60),)
    tick(conn, clock, polls)
    conn.execute("UPDATE jobs SET status = 'done' WHERE type = 'email.poll'")
    clock.advance(60)
    assert tick(conn, clock, polls) != []
