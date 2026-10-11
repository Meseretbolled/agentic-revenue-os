"""Gmail IMAP behind the Inbox port.

It asks Gmail for exactly two kinds of message and nothing else:
  1. messages whose In-Reply-To or References name one of OUR Message-IDs, and
  2. messages from an allowlisted prospect address.
Everything else in the mailbox is never fetched, never stored and never logged. Messages we
deliver are labelled `revos-processed`, and the search excludes that label, so each is delivered
once. (The orchestrator also ignores a Message-ID it has already handled.)"""

from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from revos.adapters.gmail.mime import parse_message
from revos.adapters.gmail.transport import INBOX, PROCESSED_LABEL, Mailbox
from revos.ports.mail import InboundEmail

MAX_THREAD_IDS = 100


class GmailInbox:
    def __init__(self, mailbox: Mailbox, *, our_message_ids: Callable[[], Iterable[str]],
                 allowed_senders: frozenset[str],
                 not_before: Callable[[], datetime | None] = lambda: None) -> None:
        """`not_before`: mail dated earlier than this (the first email we sent) is old mail from the
        same address, not a reply: it is not even searched for, and is dropped if the server returns it."""
        self._not_before = not_before
        self._mailbox = mailbox
        self._ours = our_message_ids
        self._senders = frozenset(a.strip().lower() for a in allowed_senders)
        self._uids: dict[str, int] = {}

    def __repr__(self) -> str:
        return "GmailInbox()"

    def poll(self) -> list[InboundEmail]:
        ours = list(dict.fromkeys(self._ours()))[-MAX_THREAD_IDS:]
        candidates: set[int] = set()
        floor = self._not_before()
        for sender in sorted(self._senders):
            candidates |= set(self._mailbox.uids_from(INBOX, sender, floor.date() if floor else None))
        for message_id in ours:
            candidates |= set(self._mailbox.uids_replying_to(INBOX, message_id))
        mine = set(ours)
        delivered: list[InboundEmail] = []
        for uid in sorted(candidates):
            email = parse_message(self._mailbox.fetch(INBOX, uid))
            relevant = email.from_addr in self._senders or bool(
                mine & {*(email.references or ()), *([email.in_reply_to] if email.in_reply_to else [])})
            if not relevant or not email.message_id or (floor and _aware(email.received_at) < floor):
                continue                       # the server matched loosely: drop it, keep nothing
            self._uids[email.message_id] = uid
            delivered.append(email)
        return delivered

    def mark_processed(self, message_id: str) -> None:
        uid = self._uids.get(message_id)
        if uid is None:
            found = self._mailbox.uids_with_message_id(INBOX, message_id)
            if not found:
                return
            uid = found[0]
        self._mailbox.add_label(INBOX, uid, PROCESSED_LABEL)
        self._uids.pop(message_id, None)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
