import random

import pytest

from revos.adapters.mock_provider import MockActionProvider
from revos.domain.errors import ActionNotApproved
from revos.runtime.events import record_event
from revos.runtime.outbox import enqueue_action, execute_action
from revos.runtime.worker import Worker


def approved_email(conn, key: str = "email:deal1:step1") -> object:
    contact = conn.execute(
        "INSERT INTO contacts (email) VALUES ('who@example.test') RETURNING id").fetchone()[0]
    action = enqueue_action(conn, key, "send_email", {"subject": "hi"},
                            autonomy_level="auto", contact_id=contact)
    conn.execute("UPDATE actions SET status = 'approved', decision = 'approved', "
                 "decision_by = 'policy', decided_at = now() WHERE id = %s", (action,))
    return action


def test_enqueue_action_twice_gives_one_action(conn) -> None:
    contact = conn.execute("INSERT INTO contacts (email) VALUES ('a@example.test') RETURNING id").fetchone()[0]
    a = enqueue_action(conn, "k", "send_email", {}, autonomy_level="auto", contact_id=contact)
    b = enqueue_action(conn, "k", "send_email", {}, autonomy_level="auto", contact_id=contact)
    assert a == b
    assert conn.execute("SELECT count(*) FROM actions").fetchone()[0] == 1


def test_happy_path_sends_once_and_records_provider_id(conn, clock) -> None:
    action = approved_email(conn)
    provider = MockActionProvider()
    pid = execute_action(conn, action, provider, clock)
    assert provider.performed == ["email:deal1:step1"]
    row = conn.execute("SELECT status, provider_id FROM actions WHERE id = %s", (action,)).fetchone()
    assert row == ("sent", pid)
    assert execute_action(conn, action, provider, clock) == pid      # re-run is a no-op
    assert len(provider.performed) == 1


def test_unapproved_action_never_reaches_the_provider(conn, clock) -> None:
    contact = conn.execute("INSERT INTO contacts (email) VALUES ('b@example.test') RETURNING id").fetchone()[0]
    action = enqueue_action(conn, "k", "send_email", {}, autonomy_level="needs_approval", contact_id=contact)
    provider = MockActionProvider()
    with pytest.raises(ActionNotApproved):
        execute_action(conn, action, provider, clock)
    assert provider.performed == [] and provider.find_calls == []


# 4. crash after the provider accepted, then retry: no double send -----------------------

def test_retry_after_crash_checks_the_provider_and_does_not_resend(conn, clock) -> None:
    action = approved_email(conn)
    provider = MockActionProvider(crash_after_perform=True)

    with pytest.raises(ConnectionError):                       # first attempt: sent, then crash
        execute_action(conn, action, provider, clock)
    assert provider.performed == ["email:deal1:step1"]
    assert conn.execute("SELECT status FROM actions WHERE id = %s", (action,)).fetchone()[0] == "approved"

    pid = execute_action(conn, action, provider, clock)         # retry
    assert provider.performed == ["email:deal1:step1"]          # still ONE send
    assert provider.find_calls == ["email:deal1:step1"] * 2
    assert conn.execute("SELECT status, provider_id FROM actions WHERE id = %s", (action,)).fetchone() == ("sent", pid)
    detail = conn.execute("SELECT detail FROM audit_log WHERE event = 'action.sent'").fetchone()[0]
    assert detail["recovered_from_provider"] is True


def test_crash_and_retry_through_the_worker_queue(conn, clock) -> None:
    action = approved_email(conn)
    provider = MockActionProvider(crash_after_perform=True)
    record_event(conn, "send:1", "action.send", {"action_id": str(action)}, clock)

    def handler(c, job):
        execute_action(c, action, provider, clock)

    worker = Worker({"action.send": handler}, clock, rng=random.Random(3))
    worker.run_once(conn)                                       # crashes, job goes back to queue
    clock.advance(60)
    worker.run_once(conn)                                       # retry succeeds
    assert provider.performed == ["email:deal1:step1"]
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "done"
    assert conn.execute("SELECT status FROM actions").fetchone()[0] == "sent"
