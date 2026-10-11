"""Live evidence for reply threading. Run it with your real .env:

    .venv/bin/python scripts/gmail_header_check.py            # send, then wait for your reply
    .venv/bin/python scripts/gmail_header_check.py --no-wait  # send and show the Sent copy only

It sends ONE email to PROSPECT_TEST_EMAIL (with the deal tag in Reply-To, as the app does), then
prints Message-ID, In-Reply-To, References, To and Reply-To for the Sent copy and, once you have
replied by hand from that inbox, for the fetched reply. Header values are not secrets; the app
password and the message bodies are never printed. Only mail that replies to ours or comes from
PROSPECT_TEST_EMAIL is read."""

import argparse
import os
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser

from revos.adapters.gmail.inbox import GmailInbox
from revos.adapters.gmail.mailer import GmailMailer
from revos.adapters.gmail.mime import key_token
from revos.adapters.gmail.transport import SENT, ImapMailbox, SmtplibTransport
from revos.composition import build_identity
from revos.env import load_env_file
from revos.ports.mail import OutboundEmail, message_id_for

HEADERS = ("Message-ID", "In-Reply-To", "References", "To", "Reply-To", "Delivered-To")


def show(title: str, raw: bytes) -> None:
    msg = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    print(f"\n{title}")
    for name in HEADERS:
        print(f"  {name + ':':<13} {msg[name] if msg[name] else '(absent)'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-wait", action="store_true", help="show the Sent copy and stop")
    parser.add_argument("--wait", type=int, default=300, help="seconds to wait for your reply")
    args = parser.parse_args()

    load_env_file()
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    prospect = os.environ.get("PROSPECT_TEST_EMAIL", "").strip().lower()
    if not (address and password and prospect and os.environ.get("POSTAL_ADDRESS", "").strip()):
        print("Set GMAIL_ADDRESS, GMAIL_APP_PASSWORD, PROSPECT_TEST_EMAIL and POSTAL_ADDRESS in .env.")
        return 2

    identity = build_identity()
    mailbox = ImapMailbox(address, password)
    mailer = GmailMailer(address, smtp=SmtplibTransport(address, password), mailbox=mailbox,
                         allowlist=frozenset({address.lower(), prospect}),
                         required_footer=(identity.unsubscribe_line, identity.postal_address),
                         sender_name=identity.sender_name)
    key = f"header-check:{uuid.uuid4()}"
    body = ("Header check for agentic-revenue-os (a fictional demo message). Please reply to this "
            f"email.\n\n{identity.unsubscribe_line}\n{identity.postal_address}")
    ours = mailer.send(OutboundEmail(key, prospect, "revos header check: please reply", body,
                                     deal_tag="d1a2b3c4"))
    print(f"We set Message-ID {ours} (derived from the key: {ours == message_id_for(key)}).")

    uids: list[int] = []
    for _ in range(20):
        uids = mailbox.sent_uids(ours, key_token(key))
        if uids:
            break
        time.sleep(3)
    if not uids:
        print("The Sent copy is not visible after 60 seconds.")
        return 1
    show("SENT COPY (what Gmail stored)", mailbox.fetch(SENT, uids[0]))
    real = mailer.actual_message_id(key)
    print(f"\nGmail kept our Message-ID: {real == ours}" + ("" if real == ours else f"  (it is {real})"))
    if args.no_wait:
        return 0

    print(f"\nNow reply to that email by hand from {prospect}. Waiting up to {args.wait}s...")
    started = datetime.now(UTC) - timedelta(minutes=2)
    inbox = GmailInbox(mailbox, our_message_ids=lambda: [i for i in (ours, real) if i],
                       allowed_senders=frozenset({prospect}), not_before=lambda: started)
    deadline = time.time() + args.wait
    found = []
    while not found and time.time() < deadline:
        found = inbox.poll()
        if not found:
            time.sleep(10)
    if not found:
        print("No reply found in time.")
        return 1
    ids = {ours, real}
    for mail in found:
        print(f"\nFETCHED REPLY from {mail.from_addr}")
        print(f"  Message-ID:   {mail.message_id}")
        print(f"  In-Reply-To:  {mail.in_reply_to or '(absent)'}")
        print(f"  References:   {' '.join(mail.references) or '(absent)'}")
        print(f"  To:           {', '.join(mail.to_addrs) or mail.to}")
        thread = bool(ids & {*mail.references, mail.in_reply_to})
        tag = any("+d1a2b3c4@" in a for a in mail.to_addrs)
        print(f"  matched by thread headers: {thread};  by plus tag in To/Delivered-To: {tag}")
        inbox.mark_processed(mail.message_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
