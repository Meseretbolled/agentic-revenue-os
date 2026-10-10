"""LIVE: talks to the real Gmail with the credentials in .env. Skipped unless run with -m live
(`make live-gmail`). It sends ONE email, from the owner's address to PROSPECT_TEST_EMAIL, and checks
that find_sent finds it again from the idempotency key alone (what makes a retry after a crash safe).
It reads no other mail."""

import os
import time
import uuid

import pytest

from revos.adapters.gmail.mailer import GmailMailer
from revos.adapters.gmail.transport import ImapMailbox, SmtplibTransport
from revos.composition import build_identity
from revos.env import load_env_file
from revos.ports.mail import OutboundEmail, message_id_for

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def mailer() -> tuple[GmailMailer, str]:
    os.environ.pop("REVOS_NO_DOTENV", None)          # the only place the tests read the real .env
    load_env_file()
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    prospect = os.environ.get("PROSPECT_TEST_EMAIL", "").strip()
    if not (address and password and prospect):
        pytest.skip("GMAIL_ADDRESS, GMAIL_APP_PASSWORD and PROSPECT_TEST_EMAIL must be set")
    if not os.environ.get("POSTAL_ADDRESS", "").strip():
        pytest.skip("POSTAL_ADDRESS must be set: every email carries it")
    identity = build_identity()
    gmail = GmailMailer(address, smtp=SmtplibTransport(address, password),
                        mailbox=ImapMailbox(address, password),
                        allowlist=frozenset({address.lower(), prospect.lower()}),
                        required_footer=(identity.unsubscribe_line, identity.postal_address),
                        sender_name=identity.sender_name)
    return gmail, prospect


def eventually(check, seconds: float = 60.0):
    """Gmail can take a few seconds to show a sent message in the Sent folder."""
    deadline = time.time() + seconds
    while True:
        value = check()
        if value or time.time() > deadline:
            return value
        time.sleep(3)


def test_one_email_is_sent_and_found_again_by_its_key(mailer) -> None:
    gmail, prospect = mailer
    identity = build_identity()
    key = f"live-gmail:{uuid.uuid4()}"
    body = ("This is the live Gmail check for agentic-revenue-os. It is a fictional demo message.\n\n"
            f"{identity.unsubscribe_line}\n{identity.postal_address}")
    sent = gmail.send(OutboundEmail(key, prospect, "revos live check", body))
    assert sent == message_id_for(key)
    assert eventually(lambda: gmail.find_sent(key)) == sent
    assert gmail.find_sent(f"live-gmail:{uuid.uuid4()}") is None      # an unknown key is not found
