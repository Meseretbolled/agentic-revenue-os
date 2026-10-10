"""Gmail (a fake one) wired through the orchestrator: send, retry after a crash, thread, poll."""

import random

import pytest

from revos.adapters.gmail.inbox import GmailInbox
from revos.adapters.gmail.mailer import GmailMailer
from revos.adapters.gmail.transport import INBOX, MailUnavailable
from revos.ports.mail import message_id_for
from revos.runtime.events import record_event
from revos.runtime.scheduler import tick
from revos.runtime.worker import Worker
from tests.fakes.gmail import FakeGmail
from tests.pipeline_helpers import CONTACT_EMAIL, audit, make_lead, make_rig

ME = "owner@gmail.test"
FOOTER = ("To stop receiving these emails, reply STOP.", "1 Example Street, Sampletown")


def gmail_rig(conn, ro_conn, pack, clock, **kw):
    server = FakeGmail()
    mailer = GmailMailer(ME, smtp=server.smtp(), mailbox=server.mailbox(),
                         allowlist=frozenset({ME, CONTACT_EMAIL, *kw.get("allowed", ())}),
                         required_footer=FOOTER, sender_name="Jo Rivers")

    def ours() -> list[str]:
        return [r[0] for r in conn.execute("SELECT message_id FROM messages WHERE direction = 'outbound' "
                                           "AND message_id IS NOT NULL ORDER BY id DESC LIMIT 100")]

    inbox = GmailInbox(server.mailbox(), our_message_ids=ours, allowed_senders=frozenset({CONTACT_EMAIL}))
    rig = make_rig(conn, ro_conn, pack, clock, mailer=mailer, inbox=inbox, **kw)
    return rig, server


def first_touch_sent(rig):
    first = rig.orch.handle_lead_created(rig.deal_id)
    assert rig.orch.send_action(first.action_id).status == "sent"
    return first


def test_the_first_touch_goes_out_through_gmail_to_the_contact_row_address(conn, ro_conn, pack, clock):
    rig, server = gmail_rig(conn, ro_conn, pack, clock)
    first = first_touch_sent(rig)
    (sent,) = server.submitted
    key = conn.execute("SELECT idempotency_key FROM actions WHERE id = %s", (first.action_id,)).fetchone()[0]
    assert sent["To"] == CONTACT_EMAIL and sent["Message-ID"] == message_id_for(key)
    body = sent.get_body(("plain",)).get_content()
    assert FOOTER[0] in body and FOOTER[1] in body
    assert conn.execute("SELECT message_id FROM messages WHERE direction = 'outbound'").fetchone()[0] \
        == message_id_for(key)


def test_a_crash_after_gmail_accepted_the_mail_is_recovered_by_find_sent_not_resent(conn, ro_conn, pack, clock):
    rig, server = gmail_rig(conn, ro_conn, pack, clock)
    first = rig.orch.handle_lead_created(rig.deal_id)
    server.crash_after_submit = True
    with pytest.raises(MailUnavailable):
        rig.orch.send_action(first.action_id)
    assert len(server.submitted) == 1 and conn.execute(
        "SELECT status FROM actions WHERE id = %s", (first.action_id,)).fetchone()[0] == "approved"
    assert rig.orch.send_action(first.action_id).status == "sent"             # the retry
    assert len(server.submitted) == 1                                          # found, not re-sent
    assert audit(conn, "action.sent", rig.deal_id)[-1]["recovered_from_provider"] is True


def test_the_worker_retries_a_dropped_connection_without_a_second_email(conn, ro_conn, pack, clock):
    rig, server = gmail_rig(conn, ro_conn, pack, clock)
    rig.orch.handle_lead_created(rig.deal_id)
    server.crash_after_submit = True
    worker = Worker(rig.orch.handlers(), clock, rng=random.Random(1))
    worker.run_once(conn)                                    # the send job fails: the connection dropped
    clock.advance(60)
    while worker.run_once(conn):
        pass
    assert len(server.submitted) == 1
    assert conn.execute("SELECT stage FROM deals").fetchone()[0] == "outreach"


def test_a_reply_is_found_matched_to_its_deal_handled_and_marked_delivered_once(conn, ro_conn, pack, clock):
    rig, server = gmail_rig(conn, ro_conn, pack, clock)
    first_touch_sent(rig)
    ours = server.sent_ids()[0]
    server.deliver(message_id="<reply1@prospect.test>", from_addr=CONTACT_EMAIL, subject="Re: hello",
                   body="Your pricing is too expensive for us", in_reply_to=ours, references=(ours,))
    strangers = [server.deliver(message_id=f"<s{i}@x>", from_addr=f"news{i}@spam.test",
                                subject=f"PRIVATE {i}", body="newsletter") for i in range(4)]
    record_event(conn, "poll:1", "email.poll", {}, clock)
    worker = Worker(rig.orch.handlers(), clock, rng=random.Random(1))
    while worker.run_once(conn):
        pass

    assert conn.execute("SELECT classification FROM messages WHERE message_id = '<reply1@prospect.test>'"
                        ).fetchone()[0] == "price_objection"
    assert conn.execute("SELECT stage FROM deals").fetchone()[0] == "replied"
    assert {uid for _, uid in server.fetched}.isdisjoint(strangers)           # never read
    assert conn.execute("SELECT count(*) FROM messages WHERE subject LIKE 'PRIVATE%'").fetchone()[0] == 0
    runs = conn.execute("SELECT count(*) FROM agent_runs WHERE agent = 'reply'").fetchone()[0]

    record_event(conn, "poll:2", "email.poll", {}, clock)                      # the next poll window
    while worker.run_once(conn):
        pass
    assert conn.execute("SELECT count(*) FROM agent_runs WHERE agent = 'reply'").fetchone()[0] == runs
    assert conn.execute("SELECT count(*) FROM messages WHERE direction = 'inbound'").fetchone()[0] == 1
    labelled = [i for i in server.folders[INBOX] if "revos-processed" in i.labels]
    assert [i.headers()["Message-ID"] for i in labelled] == ["<reply1@prospect.test>"]


def test_polling_is_a_scheduled_event_with_one_key_per_window(conn, ro_conn, pack, clock):
    keys = tick(conn, clock)
    assert any(k.startswith("schedule:email.poll:") for k in keys)
    assert not any(k.startswith("schedule:email.poll:") for k in tick(conn, clock))     # same window
    clock.advance(61)
    assert any(k.startswith("schedule:email.poll:") for k in tick(conn, clock))         # the next one


def test_mail_from_the_prospect_without_thread_headers_is_matched_by_address(conn, ro_conn, pack, clock):
    rig, server = gmail_rig(conn, ro_conn, pack, clock)
    first_touch_sent(rig)
    server.deliver(message_id="<fresh@prospect.test>", from_addr=CONTACT_EMAIL, subject="a new thread",
                   body="Sounds good, let's talk next week")
    results = rig.orch.poll_replies()
    assert [r.label for r in results] == ["interested"]


def test_allowlisted_mail_that_matches_no_deal_is_dropped_and_audited_without_its_content(
        conn, ro_conn, pack, clock):
    rig, server = gmail_rig(conn, ro_conn, pack, clock)                         # no first touch yet
    conn.execute("UPDATE contacts SET email = 'someone.else@x.test'")
    server.deliver(message_id="<orphan@prospect.test>", from_addr=CONTACT_EMAIL, subject="ORPHAN SECRET",
                   body="who am I writing to")
    assert rig.orch.poll_replies() == []
    assert audit(conn, "inbound.unmatched") == [{"message_id": "<orphan@prospect.test>"}]
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    assert "ORPHAN SECRET" not in str(conn.execute("SELECT detail FROM audit_log").fetchall())
    assert rig.orch.poll_replies() == []                                       # and not delivered again


def test_a_failure_while_handling_a_reply_leaves_it_unmarked_so_it_is_not_lost(conn, ro_conn, pack, clock,
                                                                               monkeypatch):
    rig, server = gmail_rig(conn, ro_conn, pack, clock)
    first_touch_sent(rig)
    ours = server.sent_ids()[0]
    server.deliver(message_id="<r@prospect.test>", from_addr=CONTACT_EMAIL, subject="Re", body="sounds good",
                   in_reply_to=ours, references=(ours,))
    monkeypatch.setattr(rig.orch, "handle_reply", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    with pytest.raises(RuntimeError):
        rig.orch.poll_replies()
    assert not [i for i in server.folders[INBOX] if i.labels]
    monkeypatch.undo()
    assert len(rig.orch.poll_replies()) == 1


# --- threading, end to end ---------------------------------------------------------------------------------------

def test_the_case_study_answer_is_threaded_under_the_reply_and_the_thread_so_far(conn, ro_conn, pack, clock):
    rig, server = gmail_rig(conn, ro_conn, pack, clock)
    first_touch_sent(rig)
    ours = server.sent_ids()[0]
    server.deliver(message_id="<their1@prospect.test>", from_addr=CONTACT_EMAIL, subject="Re: hello",
                   body="Your pricing is too expensive for us", in_reply_to=ours, references=(ours,))
    rig.orch.poll_replies()
    answer = conn.execute("SELECT id FROM actions WHERE type = 'follow_up'").fetchone()[0]
    assert rig.orch.send_action(answer).status == "sent"
    reply_mail = server.submitted[-1]
    assert reply_mail["In-Reply-To"] == "<their1@prospect.test>"
    assert reply_mail["References"].split() == [ours, "<their1@prospect.test>"]
    assert reply_mail["Subject"].startswith("Re: ")


def test_a_follow_up_with_no_reply_is_threaded_under_our_own_last_message(conn, ro_conn, pack, clock):
    rig, server = gmail_rig(conn, ro_conn, pack, clock)
    first_touch_sent(rig)
    ours = server.sent_ids()[0]
    run = rig.orch.run_agent(rig.deal_id, "outreach", "outreach.2", input_={"kind": "follow_up"})
    (applied,) = rig.orch.apply_run(run, rig.deal_id)
    rig.orch.send_action(applied["action_id"])
    follow_up = server.submitted[-1]
    assert follow_up["In-Reply-To"] == ours and follow_up["References"].split() == [ours]


def test_the_confirmation_carries_the_invite_and_the_thread(conn, ro_conn, pack, clock):
    from datetime import datetime
    rig, server = gmail_rig(conn, ro_conn, pack, clock)
    first_touch_sent(rig)
    ours = server.sent_ids()[0]
    server.deliver(message_id="<i1@prospect.test>", from_addr=CONTACT_EMAIL, subject="Re", in_reply_to=ours,
                   body="Sounds good, let's talk next week", references=(ours,))
    rig.orch.poll_replies()
    for (action_id,) in conn.execute("SELECT id FROM actions WHERE status = 'approved' AND type IN "
                                     "('follow_up', 'crm_note')").fetchall():
        rig.orch.send_action(action_id)
    slot = datetime.fromisoformat(conn.execute("SELECT fields->'offered_slots'->>0 FROM deals").fetchone()[0])
    server.deliver(message_id="<a1@prospect.test>", from_addr=CONTACT_EMAIL, subject="Re", in_reply_to=ours,
                   body=f"{slot:%A %d %B} at {slot:%H:%M} works for me", references=(ours,))
    rig.orch.poll_replies()
    for (action_id,) in conn.execute("SELECT id FROM actions WHERE status = 'approved' AND type IN "
                                     "('follow_up', 'book_meeting')").fetchall():
        rig.orch.send_action(action_id)
    confirmation = server.submitted[-1]
    assert confirmation["In-Reply-To"] == "<a1@prospect.test>"
    (ics,) = list(confirmation.iter_attachments())
    assert f"mailto:{CONTACT_EMAIL}" in ics.get_content()


# --- the daily cap ------------------------------------------------------------------------------------------------

def test_the_daily_send_cap_defers_the_email_that_would_pass_it(conn, ro_conn, pack, clock):
    extra = ["a.one@brightside-optical.example", "b.two@brightside-optical.example"]
    rig, server = gmail_rig(conn, ro_conn, pack, clock, daily_cap=2, allowed=extra)
    deals = [rig.deal_id] + [make_lead(conn, email=e, name=f"P{i}")[0] for i, e in enumerate(extra)]
    results = [rig.orch.handle_lead_created(d) for d in deals]
    statuses = [rig.orch.send_action(r.action_id).status for r in results]
    assert statuses == ["sent", "sent", "deferred"] and len(server.submitted) == 2
    clock.advance(86400)
    assert rig.orch.send_action(results[2].action_id).status == "sent" and len(server.submitted) == 3


# --- the composition root ---------------------------------------------------------------------------------------------

def test_without_gmail_credentials_the_mocks_are_used(conn, monkeypatch):
    from revos.adapters.mock.mailer import MockInbox, MockMailer
    from revos.composition import build_mail
    for name in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    mailer, inbox = build_mail(conn)
    assert isinstance(mailer, MockMailer) and isinstance(inbox, MockInbox)


def test_real_mail_will_not_start_without_a_postal_address(conn, monkeypatch):
    from revos.composition import build_mail
    monkeypatch.setenv("GMAIL_ADDRESS", ME)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x" * 16)
    monkeypatch.delenv("POSTAL_ADDRESS", raising=False)
    with pytest.raises(ValueError, match="POSTAL_ADDRESS"):
        build_mail(conn)


def test_with_credentials_the_gmail_adapters_are_built_without_touching_the_network(conn, monkeypatch):
    from revos.composition import build_mail
    for name, value in (("GMAIL_ADDRESS", ME), ("GMAIL_APP_PASSWORD", "SECRET-PASSWORD-1234"),
                        ("POSTAL_ADDRESS", "9 Real Road, Realtown"), ("PROSPECT_TEST_EMAIL", "P@Inbox.test"),
                        ("REVOS_ENV", "demo")):
        monkeypatch.setenv(name, value)
    mailer, inbox = build_mail(conn)
    assert isinstance(mailer, GmailMailer) and isinstance(inbox, GmailInbox)
    assert mailer._allow == frozenset({ME, "p@inbox.test"})                 # the demo allowlist
    assert "SECRET" not in repr(mailer) + repr(inbox) + repr(mailer._smtp) + repr(mailer._mailbox)
    monkeypatch.setenv("REVOS_ENV", "dev")
    assert build_mail(conn)[0]._allow is None


def test_the_running_system_is_assembled_from_the_environment(conn, ro_conn, monkeypatch):
    from revos.adapters.mock.crm import MockCRM
    from revos.composition import build_orchestrator
    for name in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "HUBSPOT_TOKEN", "LLM_MODE"):
        monkeypatch.delenv(name, raising=False)
    orch = build_orchestrator(conn, ro_conn)
    assert orch.crm_sync is True and orch.daily_cap == 20 and orch.inbox is not None
    assert "email.poll" in orch.handlers() and isinstance(orch.provider._routes["crm_update"], MockCRM)
