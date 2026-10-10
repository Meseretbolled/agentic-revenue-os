"""Sends held by the kill switch come back by themselves when it is turned off: none lost, none twice."""

import random

from typer.testing import CliRunner

from revos.cli.main import app
from revos.policy.settings import (
    kill_switch_on,
    release_held_sends,
    resume_after_kill_switch,
    set_kill_switch,
)
from revos.runtime.scheduler import tick
from revos.runtime.worker import Worker
from tests.pipeline_helpers import CONTACT_EMAIL, audit, make_lead, make_rig

EMAILS = [CONTACT_EMAIL, "pat.jones@brightside-optical.example", "kim.ode@brightside-optical.example"]


def three_approved_sends(conn, ro_conn, pack, clock):
    rig = make_rig(conn, ro_conn, pack, clock)
    rig.orch.allowed_recipients = frozenset(EMAILS)
    deals = [rig.deal_id]
    for email, name in zip(EMAILS[1:], ("Pat Jones", "Kim Ode"), strict=True):
        deals.append(make_lead(conn, email=email, name=name)[0])
    for deal in deals:
        assert rig.orch.handle_lead_created(deal).status == "action_proposed"
    return rig, deals


def sends(conn):
    return conn.execute("SELECT status, hold_reason FROM actions WHERE type = 'first_touch' "
                        "ORDER BY created_at").fetchall()


def drain(worker, conn) -> int:
    ran = 0
    while worker.run_once(conn):
        ran += 1
    return ran


def test_switch_on_three_sends_wait_switch_off_exactly_three_sends_happen_once_each(
        conn, ro_conn, pack, clock):
    rig, deals = three_approved_sends(conn, ro_conn, pack, clock)
    worker = Worker(rig.orch.handlers(), clock, rng=random.Random(1))

    set_kill_switch(conn, True, "owner")
    assert drain(worker, conn) == 3                                   # the three send jobs run
    assert rig.world.mailer.sent == []                                # ...and all three wait
    assert sends(conn) == [("approved", "kill_switch")] * 3           # nothing failed, nothing lost

    released = resume_after_kill_switch(conn, clock, "owner")
    assert len(released) == 3 and kill_switch_on(conn) is False
    assert drain(worker, conn) == 3                                   # re-queued by itself
    assert sorted(m.to for m in rig.world.mailer.sent) == sorted(EMAILS)
    assert len({m.idempotency_key for m in rig.world.mailer.sent}) == 3
    assert sends(conn) == [("sent", None)] * 3
    assert all(conn.execute("SELECT stage FROM deals WHERE id = %s", (d,)).fetchone()[0] == "outreach"
               for d in deals)


def test_turning_it_off_again_or_running_the_safety_net_sends_nothing_twice(conn, ro_conn, pack, clock):
    rig, _ = three_approved_sends(conn, ro_conn, pack, clock)
    worker = Worker(rig.orch.handlers(), clock, rng=random.Random(1))
    set_kill_switch(conn, True, "owner")
    drain(worker, conn)
    resume_after_kill_switch(conn, clock, "owner")
    drain(worker, conn)
    assert len(rig.world.mailer.sent) == 3

    assert release_held_sends(conn, clock) == []                      # nothing is held any more
    assert resume_after_kill_switch(conn, clock, "owner") == []
    tick(conn, clock)                                                 # the scheduler's safety net
    drain(worker, conn)
    assert len(rig.world.mailer.sent) == 3
    assert conn.execute("SELECT count(*) FROM events WHERE idempotency_key LIKE 'resume:%'"
                        ).fetchone()[0] == 3


def test_releasing_twice_in_the_same_state_queues_each_send_once(conn, ro_conn, pack, clock):
    rig, _ = three_approved_sends(conn, ro_conn, pack, clock)
    worker = Worker(rig.orch.handlers(), clock, rng=random.Random(1))
    set_kill_switch(conn, True, "owner")
    drain(worker, conn)
    set_kill_switch(conn, False, "owner")                             # plain off, no resume call
    assert len(release_held_sends(conn, clock)) == 3
    assert release_held_sends(conn, clock) == []                      # same epoch: nothing new
    assert drain(worker, conn) == 3
    assert len(rig.world.mailer.sent) == 3


def test_the_scheduled_safety_net_recovers_a_switch_turned_off_by_hand(conn, ro_conn, pack, clock):
    rig, _ = three_approved_sends(conn, ro_conn, pack, clock)
    worker = Worker(rig.orch.handlers(), clock, rng=random.Random(1))
    set_kill_switch(conn, True, "owner")
    drain(worker, conn)
    set_kill_switch(conn, False, "owner")                             # nobody called resume
    assert tick(conn, clock)                                          # the scheduler fires
    assert drain(worker, conn) >= 4                                   # release_held, then 3 sends
    assert len(rig.world.mailer.sent) == 3 and sends(conn) == [("sent", None)] * 3


def test_nothing_is_released_while_the_switch_is_still_on(conn, ro_conn, pack, clock):
    rig, _ = three_approved_sends(conn, ro_conn, pack, clock)
    worker = Worker(rig.orch.handlers(), clock, rng=random.Random(1))
    set_kill_switch(conn, True, "owner")
    drain(worker, conn)
    assert release_held_sends(conn, clock) == []
    tick(conn, clock)
    drain(worker, conn)
    assert rig.world.mailer.sent == [] and sends(conn) == [("approved", "kill_switch")] * 3


def test_a_second_pause_and_resume_cycle_works_too(conn, ro_conn, pack, clock):
    rig, deals = three_approved_sends(conn, ro_conn, pack, clock)
    worker = Worker(rig.orch.handlers(), clock, rng=random.Random(1))
    set_kill_switch(conn, True, "owner")
    drain(worker, conn)
    resume_after_kill_switch(conn, clock, "owner")
    set_kill_switch(conn, True, "owner")                              # paused again before they send
    drain(worker, conn)
    assert rig.world.mailer.sent == []
    assert len(resume_after_kill_switch(conn, clock, "owner")) == 3   # a new epoch: queued again
    drain(worker, conn)
    assert len(rig.world.mailer.sent) == 3 and deals


def test_the_resume_is_written_to_the_audit_log(conn, ro_conn, pack, clock):
    rig, _ = three_approved_sends(conn, ro_conn, pack, clock)
    worker = Worker(rig.orch.handlers(), clock, rng=random.Random(1))
    set_kill_switch(conn, True, "owner")
    drain(worker, conn)
    resume_after_kill_switch(conn, clock, "owner")
    events = [r[0] for r in conn.execute("SELECT event FROM audit_log WHERE event LIKE 'system.%' "
                                         "ORDER BY id")]
    assert events == ["system.kill_switch", "system.kill_switch", "system.sends_resumed"]
    assert len(audit(conn, "system.sends_resumed")[0]["actions"]) == 3


def test_the_command_line_pauses_and_resumes(conn, ro_conn, pack, clock, test_db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    runner = CliRunner()
    assert "ON" in runner.invoke(app, ["kill-switch", "on"]).output and kill_switch_on(conn)
    off = runner.invoke(app, ["kill-switch", "off"])
    assert off.exit_code == 0 and "0 held send(s) re-queued" in off.output and not kill_switch_on(conn)
    assert runner.invoke(app, ["kill-switch", "maybe"]).exit_code == 2
