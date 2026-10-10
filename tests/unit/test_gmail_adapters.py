"""Gmail mailer and inbox against a fake Gmail: no network, no credentials."""

import imaplib
import logging
import smtplib
from email import policy
from email.parser import BytesParser

import pytest

from revos.adapters.gmail.inbox import GmailInbox
from revos.adapters.gmail.mailer import GmailMailer
from revos.adapters.gmail.mime import key_token, parse_message
from revos.adapters.gmail.transport import (
    INBOX,
    SENT,
    ImapMailbox,
    MailRejected,
    MailUnavailable,
    SmtplibTransport,
)
from revos.domain.errors import PermanentError
from revos.ports.mail import OutboundEmail, message_id_for
from tests.fakes.gmail import FakeGmail

ME = "owner@gmail.test"
PROSPECT = "prospect@inbox.test"
UNSUB = "To stop receiving these emails, reply STOP."
POSTAL = "1 Example Street, Sampletown"
BODY = f"Hi Sam,\n\nA short note.\n\n{UNSUB}\n{POSTAL}"


@pytest.fixture
def gmail():
    return FakeGmail()


def mailer(server, **kw):
    args = dict(smtp=server.smtp(), mailbox=server.mailbox(), allowlist=frozenset({ME, PROSPECT}),
                required_footer=(UNSUB, POSTAL), sender_name="Jo Rivers")
    return GmailMailer(ME, **{**args, **kw})


def email(key="k1", to=PROSPECT, body=BODY, **kw):
    return OutboundEmail(key, to, "Scaling local campaigns", body, **kw)


# --- sending: allowlist, footer, deterministic Message-ID ---------------------------------------------

def test_an_email_to_an_allowed_address_is_sent_with_a_deterministic_message_id(gmail) -> None:
    got = mailer(gmail).send(email("deal-1:outreach.1:0"))
    assert got == message_id_for("deal-1:outreach.1:0")
    (sent,) = gmail.submitted
    assert sent["Message-ID"] == got and sent["To"] == PROSPECT and ME in sent["From"]
    assert sent["X-Revos-Key"] == key_token("deal-1:outreach.1:0")
    assert mailer(FakeGmail()).send(email("deal-1:outreach.1:0")) == got        # a fresh process: same id


@pytest.mark.parametrize("to", ["stranger@elsewhere.test", "PROSPECT@inbox.test.evil", ""])
def test_a_recipient_off_the_allowlist_is_refused_before_anything_is_sent(gmail, to) -> None:
    with pytest.raises(MailRejected, match="allowlist"):
        mailer(gmail).send(email(to=to))
    assert gmail.submitted == []


def test_the_allowlist_ignores_case_and_covers_the_sender_too(gmail) -> None:
    mailer(gmail).send(email("a", to="Prospect@Inbox.TEST"))
    mailer(gmail).send(email("b", to=ME))
    assert len(gmail.submitted) == 2


@pytest.mark.parametrize("body", [f"Hi,\n{UNSUB}", f"Hi,\n{POSTAL}", "Hi, buy now"])
def test_an_email_without_the_unsubscribe_line_and_postal_address_is_never_sent(gmail, body) -> None:
    with pytest.raises(MailRejected, match="unsubscribe line or postal address"):
        mailer(gmail).send(email(body=body))
    assert gmail.submitted == []


def test_real_mail_cannot_be_configured_without_a_footer() -> None:
    with pytest.raises(ValueError, match="required"):
        GmailMailer(ME, smtp=None, mailbox=None, allowlist=None, required_footer=(UNSUB, ""))


def test_every_email_carries_a_list_unsubscribe_header_and_the_footer_text(gmail) -> None:
    mailer(gmail).send(email())
    sent = gmail.submitted[0]
    assert f"mailto:{ME}" in sent["List-Unsubscribe"]
    body = sent.get_body(("plain",)).get_content()
    assert UNSUB in body and POSTAL in body


def test_the_recipient_comes_only_from_the_contact_row(gmail) -> None:
    action = {"type": "first_touch", "idempotency_key": "k", "to_email": PROSPECT,
              "payload": {"subject": "S", "body": BODY, "to": "attacker@evil.test"}}
    mailer(gmail).perform(action)
    assert gmail.submitted[0]["To"] == PROSPECT
    with pytest.raises(MailRejected, match="no recipient"):
        mailer(gmail).perform({**action, "to_email": None})
    with pytest.raises(PermanentError):
        mailer(gmail).perform({**action, "type": "send_io"})


# --- threading ----------------------------------------------------------------------------------------------

def test_a_follow_up_is_threaded_with_in_reply_to_and_references(gmail) -> None:
    first, reply = message_id_for("k-first"), "<their-reply@inbox.test>"
    mailer(gmail).send(email("k-follow", in_reply_to=reply, references=(first, reply)))
    sent = gmail.submitted[0]
    assert sent["In-Reply-To"] == reply
    assert sent["References"].split() == [first, reply]          # oldest first, the answered one last


def test_references_always_end_with_the_message_being_answered(gmail) -> None:
    mailer(gmail).send(email("k2", in_reply_to="<c@x>", references=("<a@x>", "<b@x>")))
    assert gmail.submitted[0]["References"].split() == ["<a@x>", "<b@x>", "<c@x>"]


def test_a_first_touch_has_no_thread_headers(gmail) -> None:
    mailer(gmail).send(email("k3"))
    assert gmail.submitted[0]["In-Reply-To"] is None and gmail.submitted[0]["References"] is None


def test_the_calendar_invite_is_attached_to_the_confirmation(gmail) -> None:
    action = {"type": "follow_up", "idempotency_key": "k4", "to_email": PROSPECT,
              "payload": {"subject": "Confirmed", "body": BODY, "invite": {
                  "start": "2026-10-13T11:00:00+00:00", "minutes": 30, "summary": "Intro call"}}}
    mailer(gmail).perform(action)
    parts = [p for p in gmail.submitted[0].iter_attachments()]
    assert len(parts) == 1 and parts[0].get_content_type() == "text/calendar"
    assert parts[0].get_filename() == "invite.ics"
    ics = parts[0].get_content()
    assert f"ATTENDEE;RSVP=TRUE:mailto:{PROSPECT}" in ics and "DTSTART:20261013T110000Z" in ics


# --- find_sent: a retry after a crash never double-sends -------------------------------------------------------

def test_find_sent_finds_what_left_and_only_that(gmail) -> None:
    m = mailer(gmail)
    assert m.find_sent("k-new") is None
    m.send(email("k-new"))
    assert m.find_sent("k-new") == message_id_for("k-new") and m.find_sent("k-other") is None


def test_after_a_crash_following_the_smtp_accept_find_sent_still_finds_the_email(gmail) -> None:
    gmail.crash_after_submit = True
    with pytest.raises(MailUnavailable):
        mailer(gmail).send(email("k-crash"))
    restarted = mailer(gmail)                                  # a new process with nothing in memory
    assert restarted.find_sent("k-crash") == message_id_for("k-crash")
    assert len(gmail.submitted) == 1


def test_find_sent_survives_a_server_that_rewrites_the_message_id(gmail) -> None:
    gmail.rewrite_message_id = True
    mailer(gmail).send(email("k-rewrite"))
    assert gmail.sent_ids() == ["<rewritten-by-server@mail.gmail.com>"]
    assert mailer(gmail).find_sent("k-rewrite") == message_id_for("k-rewrite")      # via X-Revos-Key


def test_smtp_failures_are_split_into_never_retry_and_retry_later(gmail) -> None:
    gmail.refuse = "recipient"
    with pytest.raises(MailRejected):
        mailer(gmail).send(email())
    gmail.refuse = "disconnect"
    with pytest.raises(MailUnavailable):
        mailer(gmail).send(email())
    assert gmail.submitted == []


# --- the real transports, with the library classes stubbed ------------------------------------------------------

class StubSmtp:
    behaviour: Exception | None = None
    logins: list[tuple[str, str]] = []

    def __init__(self, host, port, timeout=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        StubSmtp.logins.append((user, password))
        if isinstance(StubSmtp.behaviour, smtplib.SMTPAuthenticationError):
            raise StubSmtp.behaviour

    def send_message(self, message):
        if StubSmtp.behaviour and not isinstance(StubSmtp.behaviour, smtplib.SMTPAuthenticationError):
            raise StubSmtp.behaviour


@pytest.mark.parametrize("failure,expected", [
    (smtplib.SMTPAuthenticationError(535, b"bad credentials"), MailRejected),
    (smtplib.SMTPRecipientsRefused({"a@b": (550, b"no")}), MailRejected),
    (smtplib.SMTPDataError(552, b"too big"), MailRejected),
    (smtplib.SMTPDataError(451, b"try later"), MailUnavailable),
    (smtplib.SMTPServerDisconnected("gone"), MailUnavailable),
    (TimeoutError("slow"), MailUnavailable),
    (smtplib.SMTPException("other"), MailUnavailable),
])
def test_smtp_errors_are_mapped_and_never_contain_the_password(failure, expected) -> None:
    StubSmtp.behaviour = failure
    transport = SmtplibTransport(ME, "SECRET-APP-PASSWORD", smtp_class=StubSmtp)
    with pytest.raises(expected) as err:
        transport.send(__import__("email.message", fromlist=["EmailMessage"]).EmailMessage())
    assert "SECRET-APP-PASSWORD" not in str(err.value) and "SECRET-APP-PASSWORD" not in repr(transport)
    assert err.value.__cause__ is None                       # the original (which may echo) is dropped


class StubImap:
    commands: list[tuple] = []
    search_result = b""

    def __init__(self, host, port, timeout=None):
        pass

    def login(self, user, password):
        if password == "wrong":
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    def select(self, folder, readonly=False):
        StubImap.commands.append(("select", folder, readonly))
        return "OK", [b"1"]

    def uid(self, command, *args):
        StubImap.commands.append((command, *args))
        if command == "FETCH":
            return "OK", [(b"1 (BODY[] {5}", b"hello"), b")"]
        return "OK", [StubImap.search_result]

    def logout(self):
        pass


def test_the_real_imap_mailbox_speaks_gmails_search_dialect() -> None:
    StubImap.commands, StubImap.search_result = [], b"7 9"
    box = ImapMailbox(ME, "pw", imap_class=StubImap)
    assert box.has_sent("<abc@revos.example>", "tok") is True
    assert ("SEARCH", None, "X-GM-RAW", '"rfc822msgid:<abc@revos.example>"') in StubImap.commands
    assert box.uids_from(INBOX, PROSPECT) == [7, 9]
    assert ("SEARCH", None, "X-GM-RAW", f'"from:{PROSPECT} -label:revos-processed"') in StubImap.commands
    box.uids_replying_to(INBOX, "<m@revos.example>")
    assert ("SEARCH", None, "X-GM-RAW", '"-label:revos-processed"', "HEADER", "In-Reply-To",
            '"<m@revos.example>"') in StubImap.commands
    assert ("SEARCH", None, "X-GM-RAW", '"-label:revos-processed"', "HEADER", "References",
            '"<m@revos.example>"') in StubImap.commands
    assert box.fetch(INBOX, 7) == b"hello" and ("FETCH", "7", "(BODY.PEEK[])") in StubImap.commands
    box.add_label(INBOX, 7, "revos-processed")
    assert ("STORE", "7", "+X-GM-LABELS", "(revos-processed)") in StubImap.commands
    assert ("select", INBOX, False) in StubImap.commands and ("select", SENT, True) in StubImap.commands


def test_a_wrong_imap_password_is_a_permanent_error_without_the_password() -> None:
    box = ImapMailbox(ME, "wrong", imap_class=StubImap)
    with pytest.raises(MailRejected) as err:
        box.uids_from(INBOX, PROSPECT)
    assert "wrong" not in str(err.value) and "wrong" not in repr(box)


# --- reply polling -----------------------------------------------------------------------------------------------

def inbox(server, ours=(), senders=frozenset({PROSPECT})):
    return GmailInbox(server.mailbox(), our_message_ids=lambda: list(ours), allowed_senders=senders)


def populate(server):
    """One thread reply, one allowlisted-sender message, one reply via References, and 5 strangers."""
    ours = message_id_for("k-first")
    ids = {
        "reply": server.deliver(message_id="<r1@x>", from_addr=PROSPECT, subject="Re: hello",
                                body="Sounds good", in_reply_to=ours, references=(ours,)),
        "sender_only": server.deliver(message_id="<r2@x>", from_addr=PROSPECT, subject="new topic", body="hi"),
        "via_refs": server.deliver(message_id="<r3@x>", from_addr="colleague@elsewhere.test", subject="fwd",
                                   body="forwarded", in_reply_to="<other@x>", references=("<a@x>", ours)),
    }
    for i in range(5):
        ids[f"stranger{i}"] = server.deliver(message_id=f"<s{i}@x>", from_addr=f"news{i}@spam.test",
                                             subject=f"SECRET SUBJECT {i}", body="newsletter")
    return ours, ids


def test_only_replies_to_our_messages_or_mail_from_the_prospect_are_fetched(gmail) -> None:
    ours, ids = populate(gmail)
    got = inbox(gmail, [ours]).poll()
    assert sorted(m.message_id for m in got) == ["<r1@x>", "<r2@x>", "<r3@x>"]
    fetched = {uid for _, uid in gmail.fetched}
    assert fetched == {ids["reply"], ids["sender_only"], ids["via_refs"]}
    assert not fetched & {ids[f"stranger{i}"] for i in range(5)}        # never even read


def test_unrelated_mail_is_never_logged_or_returned(gmail, caplog) -> None:
    ours, _ = populate(gmail)
    with caplog.at_level(logging.DEBUG):
        got = inbox(gmail, [ours]).poll()
    assert "SECRET SUBJECT" not in caplog.text and "newsletter" not in caplog.text
    assert all("SECRET" not in m.subject for m in got)


def test_a_search_that_matches_loosely_cannot_leak_unrelated_mail_through(gmail) -> None:
    ours, ids = populate(gmail)
    gmail.lax_header_search = True                    # the server also returns the strangers' threads
    gmail.deliver(message_id="<lax@x>", from_addr="other@x.test", subject="LAX SECRET", body="b",
                  in_reply_to="<not-ours@x>")
    got = inbox(gmail, [ours]).poll()
    assert "<lax@x>" not in [m.message_id for m in got]                # fetched, judged, dropped


def test_a_reply_is_parsed_with_its_thread_headers(gmail) -> None:
    ours, _ = populate(gmail)
    reply = next(m for m in inbox(gmail, [ours]).poll() if m.message_id == "<r1@x>")
    assert (reply.from_addr, reply.subject, reply.in_reply_to) == (PROSPECT, "Re: hello", ours)
    assert reply.references == (ours,) and "Sounds good" in reply.body and not reply.is_html


def test_each_reply_is_delivered_once(gmail) -> None:
    ours, _ = populate(gmail)
    box = inbox(gmail, [ours])
    first = box.poll()
    for m in first:
        box.mark_processed(m.message_id)
    assert box.poll() == []
    assert inbox(gmail, [ours]).poll() == []                  # a restarted process agrees: it is labelled
    labelled = [i for i in gmail.folders[INBOX] if "revos-processed" in i.labels]
    assert len(labelled) == 3


def test_an_unmarked_message_comes_again_so_a_failure_does_not_lose_it(gmail) -> None:
    ours, _ = populate(gmail)
    box = inbox(gmail, [ours])
    assert len(box.poll()) == 3
    assert len(box.poll()) == 3                               # nothing was marked: nothing was lost


def test_marking_works_after_a_restart_by_finding_the_message_again(gmail) -> None:
    ours, _ = populate(gmail)
    inbox(gmail, [ours]).mark_processed("<r1@x>")
    assert [m.message_id for m in inbox(gmail, [ours]).poll() if m.message_id == "<r1@x>"] == []
    inbox(gmail, [ours]).mark_processed("<does-not-exist@x>")            # unknown: quietly nothing


def test_html_only_mail_is_flagged_as_html(gmail) -> None:
    raw = (b"From: a@b.test\r\nTo: me@x.test\r\nSubject: s\r\nMessage-ID: <h@x>\r\n"
           b"Content-Type: text/html\r\n\r\n<p>hi</p>")
    parsed = parse_message(raw)
    assert parsed.is_html and "<p>hi</p>" in parsed.body and parsed.message_id == "<h@x>"
    assert BytesParser(policy=policy.default).parsebytes(raw)["Subject"] == "s"
