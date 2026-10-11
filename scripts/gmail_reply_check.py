"""Live check of the reply path. Run it with your real .env:

    .venv/bin/python scripts/gmail_reply_check.py

It sends ONE email to PROSPECT_TEST_EMAIL, then waits while you reply to it by hand from that
inbox. It then shows that the reply was fetched, matched to our thread (In-Reply-To / References
name our Message-ID) and delivered exactly once: a second poll returns nothing. It reads only
messages that reply to ours or come from PROSPECT_TEST_EMAIL, and prints no secrets and no
other mail. Nothing is stored; use --message-id to wait on a thread from an earlier run."""

import argparse
import os
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

from revos.adapters.gmail.inbox import GmailInbox
from revos.adapters.gmail.mailer import GmailMailer
from revos.adapters.gmail.transport import ImapMailbox, SmtplibTransport
from revos.composition import build_identity
from revos.env import load_env_file
from revos.ports.mail import OutboundEmail


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--message-id", help="wait on this earlier Message-ID instead of sending")
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
    if args.message_id:
        ours = args.message_id
    else:
        body = ("Live reply check for agentic-revenue-os (a fictional demo message). "
                "Please reply to this email.\n\n" f"{identity.unsubscribe_line}\n{identity.postal_address}")
        ours = mailer.send(OutboundEmail(f"reply-check:{uuid.uuid4()}", prospect,
                                         "revos reply check: please reply", body))
        print(f"Sent {ours} to {prospect}. Reply to it from that inbox now.")

    started = datetime.now(UTC) - timedelta(minutes=2)
    inbox = GmailInbox(mailbox, our_message_ids=lambda: [ours], allowed_senders=frozenset({prospect}),
                       not_before=lambda: started)       # older mail from that address is not a reply
    deadline = time.time() + args.wait
    found = []
    while not found and time.time() < deadline:
        found = inbox.poll()
        if not found:
            time.sleep(10)
    if not found:
        print(f"No reply within {args.wait}s. Re-run with --message-id {ours}")
        return 1

    for mail in found:
        threaded = ours in {*(mail.references or ()), mail.in_reply_to}
        print(f"Fetched reply {mail.message_id} from {mail.from_addr}; "
              f"matched to our thread: {'yes' if threaded else 'no (matched by sender address)'}")
        inbox.mark_processed(mail.message_id)
    again = inbox.poll()
    print(f"Second poll after marking: {len(again)} message(s). "
          + ("Delivered exactly once." if not again else "NOT once-only: investigate."))
    return 0 if not again else 1


if __name__ == "__main__":
    sys.exit(main())
