"""`revos run`'s engine: the worker and scheduler on real Postgres with the mock adapters."""

import threading
import time
from datetime import timedelta

import pytest

from revos import obs
from revos.adapters.clock import FakeClock
from revos.composition import build_orchestrator
from revos.runtime.events import record_event
from revos.runtime.supervisor import Supervisor
from revos.seed import demo_address, seed_lead

BASE = "prospect@example.com"


@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    for name in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "HUBSPOT_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_MODE", "fake")
    monkeypatch.setenv("PROSPECT_TEST_EMAIL", BASE)
    monkeypatch.setenv("REVOS_ENV", "demo")


class TickingClock(FakeClock):
    """A test clock that also moves with real time (a worker retrying after a backoff needs time to
    pass), plus whatever the test advances by hand."""

    def __init__(self) -> None:
        super().__init__()
        self._t0 = time.monotonic()

    def now(self):
        return super().now() + timedelta(seconds=time.monotonic() - self._t0)


@pytest.fixture
def clock():
    return TickingClock()


def real_handlers(conn, ro, clock):
    return build_orchestrator(conn, ro, clock).handlers()


def supervisor(url, clock, factory=real_handlers, **kw):
    return Supervisor(database_url=url, clock=clock, build_handlers=factory, tick_seconds=0.2,
                      idle_seconds=0.05, grace_seconds=kw.pop("grace_seconds", 10), **kw)


def wait_for(check, seconds=30.0, conn=None):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if (value := check()):
            return value
        time.sleep(0.1)
    state = conn.execute("SELECT type, status, attempts, last_error FROM jobs").fetchall() if conn else ""
    raise AssertionError(f"timed out waiting; jobs: {state}")


def seed(conn, clock, pack, tag="acme"):
    return seed_lead(conn, clock, segment_id=pack.id, pack_version="1", company="Brightside Optical Group",
                     domain="brightside-optical.example", contact_name="Sam Lee",
                     email=demo_address(tag))[0]


def test_run_processes_a_lead_end_to_end_and_every_row_carries_one_correlation_id(
        conn, pack, clock, test_db_url, tmp_path):
    log = obs.configure_logging(tmp_path / "revos.jsonl")
    deal_id = seed(conn, clock, pack)
    run = supervisor(test_db_url, clock)
    run.start()
    try:
        wait_for(lambda: conn.execute("SELECT stage FROM deals").fetchone()[0] in ("scored", "outreach")
                 and conn.execute("SELECT count(*) FROM actions WHERE type = 'first_touch'").fetchone()[0],
                 conn=conn)
    finally:
        report = run.stop()
        obs.disable_logging()
    assert report.finished_cleanly and not run.errors
    assert conn.execute("SELECT count(*) FROM jobs WHERE status = 'running'").fetchone()[0] == 0
    (event_cid,) = conn.execute("SELECT correlation_id FROM events WHERE type = 'lead.created'").fetchone()
    assert event_cid
    # the queue, the agents, the policy engine and the outbox all agree on it
    for table in ("jobs", "actions", "agent_runs"):
        cids = {r[0] for r in conn.execute(
            f"SELECT correlation_id FROM {table} WHERE correlation_id IS NOT NULL "
            + ("AND type = 'lead.created'" if table == "jobs" else ""))}
        assert event_cid in cids
    policy_cids = {r[0] for r in conn.execute("SELECT correlation_id FROM audit_log WHERE event = "
                                              "'policy.decided'")}
    assert policy_cids == {event_cid}
    logged = {r["correlation_id"] for r in obs.read_log(log, deal_id=str(deal_id))}
    assert event_cid in logged and {r["component"] for r in obs.read_log(log, deal_id=str(deal_id))} >= {
        "queue", "audit", "transitions"}


def test_a_restart_finishes_what_the_first_run_left_and_sends_exactly_once(conn, pack, clock, test_db_url):
    seed(conn, clock, pack)
    first = supervisor(test_db_url, clock)
    first.start()
    wait_for(lambda: conn.execute("SELECT count(*) FROM actions WHERE type = 'first_touch'").fetchone()[0])
    first.stop()
    clock.advance(180)                                    # past the veto window, still in the send window
    second = supervisor(test_db_url, clock)
    second.start()
    try:
        wait_for(lambda: conn.execute("SELECT status FROM actions WHERE type = 'first_touch'"
                                      ).fetchone()[0] == "sent")
    finally:
        second.stop()
    assert conn.execute("SELECT count(*) FROM messages WHERE direction = 'outbound'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM jobs WHERE status IN ('running', 'dead')").fetchone()[0] == 0
    assert conn.execute("SELECT to_email FROM (SELECT c.email AS to_email FROM actions a JOIN contacts c "
                        "ON c.id = a.contact_id WHERE a.type = 'first_touch') t").fetchone()[0] \
        == "prospect+acme@example.com"


def test_a_job_that_cannot_finish_in_the_grace_period_is_released_not_lost(conn, clock, test_db_url):
    started, release = threading.Event(), threading.Event()

    def slow(_conn, _job):
        started.set()
        release.wait(20)

    run = supervisor(test_db_url, clock, factory=lambda c, r, k: {"test.slow": slow}, grace_seconds=0.3)
    record_event(conn, "slow:1", "test.slow", {}, clock)
    run.start()
    assert started.wait(10)
    report = run.stop()
    assert not report.finished_cleanly and len(report.released_job_ids) == 1
    job = conn.execute("SELECT status, attempts, lease_until FROM jobs WHERE type = 'test.slow'").fetchone()
    assert job == ("queued", 0, None)                     # back on the queue, attempt returned
    release.set()
    time.sleep(0.3)
    assert conn.execute("SELECT status FROM jobs WHERE type = 'test.slow'").fetchone()[0] == "queued"  # fenced


def test_a_job_that_finishes_inside_the_grace_period_is_completed(conn, clock, test_db_url):
    started = threading.Event()

    def brief(_conn, _job):
        started.set()
        time.sleep(0.4)

    run = supervisor(test_db_url, clock, factory=lambda c, r, k: {"test.brief": brief}, grace_seconds=10)
    record_event(conn, "brief:1", "test.brief", {}, clock)
    run.start()
    assert started.wait(10)
    report = run.stop()
    assert report.finished_cleanly and conn.execute("SELECT status FROM jobs WHERE type = 'test.brief'").fetchone()[0] == "done"


def test_the_scheduler_enqueues_each_recurring_event_once_even_with_two_schedulers(conn, clock, test_db_url):
    a = supervisor(test_db_url, clock, factory=lambda c, r, k: {})
    b = supervisor(test_db_url, clock, factory=lambda c, r, k: {})
    a.start()
    b.start()
    try:
        wait_for(lambda: conn.execute("SELECT count(*) FROM events WHERE idempotency_key LIKE 'schedule:%'"
                                      ).fetchone()[0] >= 5)
        time.sleep(0.6)
    finally:
        a.stop()
        b.stop()
    kinds = dict(conn.execute("SELECT type, count(*) FROM events WHERE idempotency_key LIKE 'schedule:%' "
                              "GROUP BY type").fetchall())
    assert set(kinds) == {"email.poll", "calls.poll", "tracking.stall_scan", "tracking.digest",
                          "lessons.sweep", "queue.release_held"}
    assert set(kinds.values()) == {1}


def test_the_recurring_jobs_all_have_handlers_and_run_without_dead_letters(conn, pack, clock, test_db_url):
    run = supervisor(test_db_url, clock)
    run.start()
    try:
        wait_for(lambda: conn.execute("SELECT count(*) FROM jobs WHERE status = 'done'").fetchone()[0] >= 5)
    finally:
        run.stop()
    assert conn.execute("SELECT count(*) FROM dead_letters").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM audit_log WHERE event = 'tracking.digest'").fetchone()[0] == 1


def test_a_crash_in_the_worker_is_reported_and_stops_everything(conn, clock, test_db_url):
    def broken(*_):
        raise RuntimeError("cannot build")

    run = supervisor(test_db_url, clock, factory=broken)
    with pytest.raises(RuntimeError, match="startup failed"):
        run.start()
    assert run.errors and "cannot build" in run.errors[0]


def test_seeding_is_idempotent_and_each_plus_address_is_its_own_contact(conn, pack, clock):
    first = seed(conn, clock, pack, "acme")
    again = seed(conn, clock, pack, "acme")
    other = seed(conn, clock, pack, "globex")
    assert first == again and first != other
    assert conn.execute("SELECT count(*) FROM events WHERE type = 'lead.created'").fetchone()[0] == 2
    assert sorted(r[0] for r in conn.execute("SELECT email FROM contacts")) == [
        "prospect+acme@example.com", "prospect+globex@example.com"]


def test_demo_addresses_are_plus_variants_of_the_prospect_inbox_and_reject_junk_tags(monkeypatch):
    monkeypatch.setenv("PROSPECT_TEST_EMAIL", "Me+old@Gmail.com")
    assert demo_address("Acme Corp!") == "me+acmecorp@gmail.com"
    with pytest.raises(ValueError):
        demo_address("***")
