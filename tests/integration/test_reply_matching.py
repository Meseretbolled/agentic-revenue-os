"""Which deal does a reply belong to? Thread headers, then the plus tag, then the sender if it is
unambiguous. All the demo leads share one prospect inbox, so the sender alone is rarely enough."""

import pytest

from revos.adapters.gmail.mailer import GmailMailer, plus_address
from revos.adapters.gmail.transport import INBOX, MailRejected
from revos.ports.mail import OutboundEmail
from tests.integration.test_gmail_loop import ME, gmail_rig
from tests.pipeline_helpers import CONTACT_EMAIL, audit, make_lead


def another_deal(conn, rig):
    """A second deal for the same contact: everything lands in one prospect inbox."""
    return conn.execute(
        "INSERT INTO deals (segment_id, company_id, contact_id, fields) "
        "SELECT segment_id, company_id, contact_id, fields FROM deals WHERE id = %s RETURNING id",
        (rig.deal_id,)).fetchone()[0]


def two_deals(conn, ro_conn, pack, clock):
    """Two open outreach deals whose contact is the same inbox, both with a first touch sent."""
    rig, server = gmail_rig(conn, ro_conn, pack, clock)
    second = another_deal(conn, rig)
    sent = {}
    for name, deal in (("a", rig.deal_id), ("b", second)):
        first = rig.orch.handle_lead_created(deal)
        assert rig.orch.send_action(first.action_id).status == "sent"
        sent[name] = deal
    ids = server.sent_ids()
    return rig, server, sent, {"a": ids[0], "b": ids[1]}


def reply(server, mid, **kw):
    server.deliver(message_id=mid, from_addr=CONTACT_EMAIL, subject="Re: hello",
                   body=kw.pop("body", "Sounds good, let's talk next week"), **kw)


def matched_by(conn, mid):
    rows = [r for r in conn.execute("SELECT detail FROM audit_log WHERE event = 'inbound.matched'")]
    return {r[0]["message_id"]: r[0]["by"] for r in rows}.get(mid)


def deal_of(conn, mid):
    return conn.execute("SELECT deal_id FROM messages WHERE message_id = %s", (mid,)).fetchone()[0]


# --- the sent mail carries what matching needs ---------------------------------------------------------------

def test_every_email_carries_a_reply_to_plus_address_with_the_deal_tag(conn, ro_conn, pack, clock):
    rig, server, deals, _ = two_deals(conn, ro_conn, pack, clock)
    tags = [m["Reply-To"] for m in server.submitted]
    assert tags == [f"owner+{str(deals['a'])[:8]}@gmail.test", f"owner+{str(deals['b'])[:8]}@gmail.test"]
    assert all(m["To"] == CONTACT_EMAIL for m in server.submitted)


def test_the_plus_address_is_never_a_recipient_and_the_allowlist_still_guards_outbound(conn):
    from tests.fakes.gmail import FakeGmail
    server = FakeGmail()
    mailer = GmailMailer(ME, smtp=server.smtp(), mailbox=server.mailbox(), allowlist=frozenset({ME}),
                         required_footer=("unsub", "addr"))
    assert plus_address("owner@gmail.test", "d1a2b3c4") == "owner+d1a2b3c4@gmail.test"
    assert plus_address("owner@gmail.test", "not hex!") is None and plus_address("owner@gmail.test", None) is None
    with pytest.raises(MailRejected):
        mailer.send(OutboundEmail("k", "owner+d1a2b3c4@gmail.test", "s", "unsub addr"))   # not on the list
    assert server.submitted == []


# --- the four match paths -----------------------------------------------------------------------------------------

def test_a_reply_is_matched_by_its_thread_headers_even_when_two_deals_share_the_inbox(conn, ro_conn, pack, clock):
    rig, server, deals, ids = two_deals(conn, ro_conn, pack, clock)
    reply(server, "<t1@p.test>", in_reply_to=ids["b"], references=(ids["b"],))
    assert len(rig.orch.poll_replies()) == 1
    assert deal_of(conn, "<t1@p.test>") == deals["b"] and matched_by(conn, "<t1@p.test>") == "thread"


def test_a_reply_to_the_id_gmail_really_assigned_is_matched_through_the_stored_real_id(conn, ro_conn, pack, clock):
    rig, server = gmail_rig(conn, ro_conn, pack, clock)
    server.rewrite_message_id = True
    second = another_deal(conn, rig)
    for deal in (rig.deal_id, second):
        first = rig.orch.handle_lead_created(deal)
        assert rig.orch.send_action(first.action_id).status == "sent"
    assert conn.execute("SELECT count(*) FROM messages WHERE real_message_id = "
                        "'<rewritten-by-server@mail.gmail.com>'").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM messages WHERE real_message_id = message_id").fetchone()[0] == 0
    reply(server, "<t2@p.test>", in_reply_to="<rewritten-by-server@mail.gmail.com>")
    # both deals' mails carry the same rewritten id here, so the thread is ambiguous: held, never guessed
    assert rig.orch.poll_replies() == []
    assert conn.execute("SELECT count(*) FROM held_replies").fetchone()[0] == 1


def test_the_real_id_alone_identifies_the_deal_and_followups_thread_under_it(conn, ro_conn, pack, clock):
    rig, server = gmail_rig(conn, ro_conn, pack, clock)
    server.rewrite_message_id = True
    first = rig.orch.handle_lead_created(rig.deal_id)
    assert rig.orch.send_action(first.action_id).status == "sent"
    real = "<rewritten-by-server@mail.gmail.com>"
    reply(server, "<t3@p.test>", in_reply_to=real, references=(real,),
          body="Your pricing is too expensive for us")
    assert len(rig.orch.poll_replies()) == 1 and matched_by(conn, "<t3@p.test>") == "thread"
    answer = conn.execute("SELECT id FROM actions WHERE type = 'follow_up'").fetchone()[0]
    assert rig.orch.send_action(answer).status == "sent"
    assert server.submitted[-1]["References"].split() == [real, "<t3@p.test>"]    # our own id is not used


def test_the_real_id_is_filled_in_at_poll_time_when_the_sent_copy_was_not_visible_at_send_time(
        conn, ro_conn, pack, clock):
    rig, server = gmail_rig(conn, ro_conn, pack, clock)
    server.rewrite_message_id = True
    server.hide_sent = True
    first = rig.orch.handle_lead_created(rig.deal_id)
    rig.orch.send_action(first.action_id)
    assert conn.execute("SELECT real_message_id FROM messages").fetchone()[0] is None
    server.hide_sent = False
    rig.orch.poll_replies()
    assert conn.execute("SELECT real_message_id FROM messages").fetchone()[0] == \
        "<rewritten-by-server@mail.gmail.com>"


def test_a_reply_with_no_thread_headers_is_matched_by_the_plus_tag_in_the_to_header(conn, ro_conn, pack, clock):
    rig, server, deals, _ = two_deals(conn, ro_conn, pack, clock)
    reply(server, "<g1@p.test>", to=f"owner+{str(deals['a'])[:8]}@gmail.test")
    assert len(rig.orch.poll_replies()) == 1
    assert deal_of(conn, "<g1@p.test>") == deals["a"] and matched_by(conn, "<g1@p.test>") == "tag"


def test_the_tag_is_also_read_from_delivered_to(conn, ro_conn, pack, clock):
    rig, server, deals, _ = two_deals(conn, ro_conn, pack, clock)
    reply(server, "<g2@p.test>", delivered_to=f"owner+{str(deals['b'])[:8]}@gmail.test")
    rig.orch.poll_replies()
    assert deal_of(conn, "<g2@p.test>") == deals["b"] and matched_by(conn, "<g2@p.test>") == "tag"


def test_the_sender_alone_is_enough_only_when_exactly_one_open_outreach_deal_exists(conn, ro_conn, pack, clock):
    rig, server = gmail_rig(conn, ro_conn, pack, clock)
    first = rig.orch.handle_lead_created(rig.deal_id)
    rig.orch.send_action(first.action_id)
    reply(server, "<s1@p.test>")
    assert len(rig.orch.poll_replies()) == 1 and matched_by(conn, "<s1@p.test>") == "sender"


def test_a_deal_not_yet_contacted_does_not_make_the_sender_ambiguous(conn, ro_conn, pack, clock):
    rig, server = gmail_rig(conn, ro_conn, pack, clock)
    first = rig.orch.handle_lead_created(rig.deal_id)
    rig.orch.send_action(first.action_id)
    another_deal(conn, rig)                                  # same inbox, nothing sent
    reply(server, "<s2@p.test>")
    assert len(rig.orch.poll_replies()) == 1 and deal_of(conn, "<s2@p.test>") == rig.deal_id


# --- the ambiguous case ----------------------------------------------------------------------------------------------------

def test_two_candidates_and_no_headers_or_tag_hold_the_reply_unmatched_and_audited(conn, ro_conn, pack, clock):
    rig, server, deals, _ = two_deals(conn, ro_conn, pack, clock)
    reply(server, "<x1@p.test>", body="Thanks, interested")
    assert rig.orch.poll_replies() == []
    assert conn.execute("SELECT count(*) FROM messages WHERE direction = 'inbound'").fetchone()[0] == 0
    (held,) = conn.execute("SELECT message_id, reason, candidate_deal_ids, body FROM held_replies").fetchall()
    assert held[:2] == ("<x1@p.test>", "ambiguous") and set(held[2]) == set(deals.values())
    (event,) = audit(conn, "inbound.ambiguous")
    assert event["message_id"] == "<x1@p.test>" and len(event["candidates"]) == 2
    assert "interested" not in str(event)                                      # the audit holds no content
    assert conn.execute("SELECT count(*) FROM agent_runs WHERE agent = 'reply'").fetchone()[0] == 0
    assert [i.headers()["Message-ID"] for i in server.folders[INBOX] if i.labels] == ["<x1@p.test>"]
    assert rig.orch.poll_replies() == []                                       # delivered once, not again
    assert conn.execute("SELECT count(*) FROM held_replies").fetchone()[0] == 1


def test_a_tag_that_matches_nothing_falls_through_to_the_next_rule(conn, ro_conn, pack, clock):
    rig, server, deals, _ = two_deals(conn, ro_conn, pack, clock)
    reply(server, "<x2@p.test>", to="owner+deadbeef@gmail.test")
    assert rig.orch.poll_replies() == []
    assert conn.execute("SELECT count(*) FROM held_replies").fetchone()[0] == 1


def test_a_thread_header_beats_a_tag_that_points_elsewhere(conn, ro_conn, pack, clock):
    rig, server, deals, ids = two_deals(conn, ro_conn, pack, clock)
    reply(server, "<x3@p.test>", in_reply_to=ids["a"], to=f"owner+{str(deals['b'])[:8]}@gmail.test")
    rig.orch.poll_replies()
    assert deal_of(conn, "<x3@p.test>") == deals["a"] and matched_by(conn, "<x3@p.test>") == "thread"


def test_every_match_path_still_delivers_exactly_once(conn, ro_conn, pack, clock):
    rig, server, deals, ids = two_deals(conn, ro_conn, pack, clock)
    reply(server, "<o1@p.test>", in_reply_to=ids["a"])
    reply(server, "<o2@p.test>", to=f"owner+{str(deals['b'])[:8]}@gmail.test")
    assert len(rig.orch.poll_replies()) == 2
    assert rig.orch.poll_replies() == [] and rig.orch.poll_replies() == []
    assert conn.execute("SELECT count(*) FROM messages WHERE direction = 'inbound'").fetchone()[0] == 2


def test_unrelated_mail_with_a_plus_tag_is_still_never_fetched(conn, ro_conn, pack, clock):
    rig, server, deals, _ = two_deals(conn, ro_conn, pack, clock)
    stranger = server.deliver(message_id="<z@x.test>", from_addr="news@spam.test", subject="PRIVATE",
                              body="hi", to=f"owner+{str(deals['a'])[:8]}@gmail.test")
    rig.orch.poll_replies()
    assert ("INBOX", stranger) not in server.fetched
    assert conn.execute("SELECT count(*) FROM held_replies").fetchone()[0] == 0


def test_trace_shows_the_held_reply_and_the_audit_entries(conn, ro_conn, pack, clock):
    from revos.trace import audit_trail, held_replies
    rig, server, deals, _ = two_deals(conn, ro_conn, pack, clock)
    reply(server, "<h1@p.test>", body="SECRET BODY")
    rig.orch.poll_replies()
    (held,) = held_replies(conn)
    assert held["message_id"] == "<h1@p.test>" and set(held["candidate_deal_ids"]) == set(deals.values())
    assert "inbound.ambiguous" in [e["event"] for e in audit_trail(conn)]
    assert "inbound.ambiguous" not in [e["event"] for e in audit_trail(conn, str(deals["a"]))]
    assert "SECRET BODY" not in str(held)                      # a listing never carries the body


def test_old_mail_from_the_prospect_address_is_not_a_reply(conn, ro_conn, pack, clock):
    from datetime import UTC, datetime

    from revos.adapters.gmail.inbox import GmailInbox
    rig, server, deals, _ = two_deals(conn, ro_conn, pack, clock)
    server.deliver(message_id="<old@p.test>", from_addr=CONTACT_EMAIL, subject="OLD MAIL", body="x",
                         date="Mon, 05 Jan 2026 10:00:00 +0000")
    fresh = server.deliver(message_id="<new@p.test>", from_addr=CONTACT_EMAIL, subject="Re", body="ok",
                           in_reply_to=server.sent_ids()[0])
    box = server.mailbox()
    inbox = GmailInbox(box, our_message_ids=lambda: server.sent_ids(), allowed_senders=frozenset({CONTACT_EMAIL}),
                       not_before=lambda: datetime(2026, 6, 1, tzinfo=UTC))
    assert [m.message_id for m in inbox.poll()] == ["<new@p.test>"]
    assert box.searched_since[-1].isoformat() == "2026-06-01"                  # the search itself is bounded
    assert ("INBOX", fresh) in server.fetched


def test_a_reply_addressed_to_two_deal_tags_is_held_not_guessed_from_the_sender(conn, ro_conn, pack, clock):
    rig, server = gmail_rig(conn, ro_conn, pack, clock, allowed=("pat@other.example",))
    other_deal, _ = make_lead(conn, email="pat@other.example",
                              name="Pat")
    for deal in (rig.deal_id, other_deal):
        rig.orch.send_action(rig.orch.handle_lead_created(deal).action_id)
    reply(server, "<d1@p.test>", to=f"owner+{str(rig.deal_id)[:8]}@gmail.test, "
                                   f"owner+{str(other_deal)[:8]}@gmail.test")
    assert rig.orch.poll_replies() == []
    (held,) = conn.execute("SELECT candidate_deal_ids FROM held_replies").fetchone()
    assert set(held) == {rig.deal_id, other_deal}


def test_a_reply_whose_thread_names_two_deals_is_held_not_guessed_from_the_sender(conn, ro_conn, pack, clock):
    rig, server = gmail_rig(conn, ro_conn, pack, clock, allowed=("pat@other.example",))
    other_deal, _ = make_lead(conn, email="pat@other.example", name="Pat")
    for deal in (rig.deal_id, other_deal):
        rig.orch.send_action(rig.orch.handle_lead_created(deal).action_id)
    a, b = server.sent_ids()
    reply(server, "<d2@p.test>", in_reply_to=a, references=(a, b))
    assert rig.orch.poll_replies() == []
    assert conn.execute("SELECT count(*) FROM held_replies").fetchone()[0] == 1
