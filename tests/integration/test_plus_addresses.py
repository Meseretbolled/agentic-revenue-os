"""Demo leads are plus-addresses of one prospect inbox: each is a distinct contact, mail to them is
allowed, anything that merely looks similar is not, and replies from the base address still find the
right lead."""

import pytest

from revos.adapters.gmail.inbox import GmailInbox
from revos.adapters.gmail.mailer import GmailMailer
from tests.fakes.gmail import FakeGmail
from tests.integration.test_gmail_loop import FOOTER, ME
from tests.pipeline_helpers import audit, make_lead, make_rig

BASE = "prospect@gmail.test"
ACME, GLOBEX = "prospect+acme@gmail.test", "prospect+globex@gmail.test"


def plus_rig(conn, ro_conn, pack, clock, email=ACME):
    server = FakeGmail()
    mailer = GmailMailer(ME, smtp=server.smtp(), mailbox=server.mailbox(),
                         allowlist=frozenset({ME}), plus_bases=frozenset({BASE}),
                         required_footer=FOOTER, sender_name="Jo Rivers")

    def ours():
        return [r[0] for r in conn.execute("SELECT message_id FROM messages WHERE direction = 'outbound'")]

    inbox = GmailInbox(server.mailbox(), our_message_ids=ours, allowed_senders=frozenset({BASE}))
    rig = make_rig(conn, ro_conn, pack, clock, mailer=mailer, inbox=inbox, email=email,
                   plus_bases=frozenset({BASE}))
    return rig, server


def contact_lead(conn, rig, email, name):
    deal, _ = make_lead(conn, email=email, name=name)
    return deal


def send_first(rig, deal):
    return rig.orch.send_action(rig.orch.handle_lead_created(deal).action_id)


def test_each_plus_addressed_lead_is_its_own_contact_and_receives_its_own_mail(conn, ro_conn, pack, clock):
    rig, server = plus_rig(conn, ro_conn, pack, clock)
    globex = contact_lead(conn, rig, GLOBEX, "Gil Obex")
    assert send_first(rig, rig.deal_id).status == "sent" and send_first(rig, globex).status == "sent"
    assert [m["To"] for m in server.submitted] == [ACME, GLOBEX]
    assert conn.execute("SELECT count(DISTINCT lower(email)) FROM contacts").fetchone()[0] == 2


@pytest.mark.parametrize("email", ["prospect2@gmail.test", "prospect+x@other.test", "pro.spect+tag@gmail.test",
                                   "prospect+a+b@gmail.test"])
def test_a_lookalike_address_never_gets_mail_through_the_gate(conn, ro_conn, pack, clock, email):
    rig, server = plus_rig(conn, ro_conn, pack, clock, email=email)
    result = send_first(rig, rig.deal_id)
    assert result.status == "refused" and result.reason == "recipient_not_allowed"
    assert server.submitted == []


def test_the_mailer_itself_also_refuses_a_lookalike(conn, ro_conn, pack, clock):
    from revos.adapters.gmail.transport import MailRejected
    from revos.ports.mail import OutboundEmail
    _, server = plus_rig(conn, ro_conn, pack, clock)
    mailer = GmailMailer(ME, smtp=server.smtp(), mailbox=server.mailbox(), allowlist=frozenset({ME}),
                         plus_bases=frozenset({BASE}), required_footer=FOOTER)
    with pytest.raises(MailRejected):
        mailer.send(OutboundEmail("k", "prospect2@gmail.test", "s", " ".join(FOOTER)))
    assert server.submitted == []


def reply(server, mid, **kw):
    server.deliver(message_id=mid, from_addr=BASE, subject="Re: hello", body="Sounds good, let's talk",
                   **kw)


def matched(conn):
    return {r[0]["message_id"]: r[0]["by"] for r in conn.execute(
        "SELECT detail FROM audit_log WHERE event = 'inbound.matched'")}


def test_a_reply_from_the_base_address_matches_a_plus_lead_by_thread_headers(conn, ro_conn, pack, clock):
    rig, server = plus_rig(conn, ro_conn, pack, clock)
    globex = contact_lead(conn, rig, GLOBEX, "Gil Obex")
    send_first(rig, rig.deal_id)
    send_first(rig, globex)
    first, second = server.sent_ids()
    reply(server, "<r1@x>", in_reply_to=second, references=(second,))
    rig.orch.poll_replies()
    assert conn.execute("SELECT deal_id FROM messages WHERE message_id = '<r1@x>'").fetchone()[0] == globex
    assert matched(conn)["<r1@x>"] == "thread" and first != second


def test_a_reply_from_the_base_address_matches_by_the_reply_to_plus_tag_without_headers(conn, ro_conn, pack,
                                                                                         clock):
    rig, server = plus_rig(conn, ro_conn, pack, clock)
    globex = contact_lead(conn, rig, GLOBEX, "Gil Obex")
    send_first(rig, rig.deal_id)
    send_first(rig, globex)
    tag = server.submitted[1]["Reply-To"]
    assert tag == f"owner+{str(globex)[:8]}@gmail.test"
    reply(server, "<r2@x>", to=tag)
    rig.orch.poll_replies()
    assert conn.execute("SELECT deal_id FROM messages WHERE message_id = '<r2@x>'").fetchone()[0] == globex
    assert matched(conn)["<r2@x>"] == "tag"


def test_the_base_sender_alone_matches_the_only_open_plus_lead_but_not_two(conn, ro_conn, pack, clock):
    rig, server = plus_rig(conn, ro_conn, pack, clock)
    send_first(rig, rig.deal_id)
    reply(server, "<r3@x>")
    rig.orch.poll_replies()
    assert matched(conn)["<r3@x>"] == "sender"
    globex = contact_lead(conn, rig, GLOBEX, "Gil Obex")
    send_first(rig, globex)
    reply(server, "<r4@x>")
    assert rig.orch.poll_replies() == []
    assert audit(conn, "inbound.ambiguous")[-1]["message_id"] == "<r4@x>"


def test_a_sender_that_is_itself_a_plus_address_is_compared_by_its_base(conn, ro_conn, pack, clock):
    from datetime import UTC, datetime

    from revos.ports.mail import InboundEmail
    rig, _ = plus_rig(conn, ro_conn, pack, clock)
    send_first(rig, rig.deal_id)
    mail = InboundEmail("<p@x>", "Prospect+Something@gmail.test", "me@x", "Re", "hi", datetime.now(UTC))
    assert rig.orch._deal_for_inbound(mail) == (rig.deal_id, "sender", [])
