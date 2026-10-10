"""The mock providers inside the real outbox and send gate."""

import pytest

from revos.adapters.mock import build_mock_world
from revos.policy.gate import send_with_gate
from revos.ports.mail import message_id_for
from revos.runtime.outbox import execute_action
from tests.helpers import CFG, good_first_touch, make_contact, propose


def queued(conn, clock, email="owner@example.test", payload=None):
    contact = make_contact(conn, email)
    action = propose(conn, clock, "k1", good_first_touch(), contact).action_id
    conn.execute("UPDATE actions SET payload = %s WHERE id = %s",
                 (__import__("json").dumps(payload or {"subject": "Hi", "body": "Body"}), action))
    return action


def test_gate_and_outbox_send_through_the_mock_mailer_to_the_database_recipient(conn, clock) -> None:
    world = build_mock_world()
    action = queued(conn, clock, payload={"subject": "Hi", "body": "Body", "to": "attacker@evil.test"})
    result = send_with_gate(conn, action, world.router, clock, CFG)
    assert result.status == "sent" and result.provider_id == message_id_for("k1")
    assert [m.to for m in world.mailer.sent] == ["owner@example.test"]


def test_crash_after_the_mail_server_accepted_does_not_send_twice(conn, clock) -> None:
    world = build_mock_world()
    world.mailer.crash_after_send = True
    action = queued(conn, clock)
    with pytest.raises(ConnectionError):
        send_with_gate(conn, action, world.router, clock, CFG)
    assert len(world.mailer.sent) == 1
    result = send_with_gate(conn, action, world.router, clock, CFG)         # the retry
    assert result.status == "sent" and len(world.mailer.sent) == 1          # found, not re-sent
    detail = conn.execute("SELECT detail FROM audit_log WHERE event = 'action.sent'").fetchone()[0]
    assert detail["recovered_from_provider"] is True


def test_execute_action_hands_the_provider_the_contact_email(conn, clock) -> None:
    seen = {}

    class Spy:
        def find(self, key):
            return None

        def perform(self, action):
            seen.update(action)
            return "id-1"

    action = queued(conn, clock, email="Owner@Example.test")
    execute_action(conn, action, Spy(), clock)
    assert seen["to_email"] == "Owner@Example.test"
