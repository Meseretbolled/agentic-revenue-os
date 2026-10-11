"""A fake Gmail for tests: an SMTP that files what it is given into Sent, and a mailbox that answers
exactly the questions the adapters ask and RECORDS every message it hands out, so a test can prove
unrelated mail was never fetched."""

import re
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate

from revos.adapters.gmail.transport import INBOX, SENT, MailRejected, MailUnavailable


@dataclass
class Stored:
    uid: int
    raw: bytes
    labels: set[str] = field(default_factory=set)

    def headers(self):
        return BytesParser(policy=policy.default).parsebytes(self.raw)


class FakeGmail:
    def __init__(self) -> None:
        self.folders: dict[str, list[Stored]] = {INBOX: [], SENT: []}
        self.submitted: list[EmailMessage] = []        # every SMTP submission, in order
        self.fetched: list[tuple[str, int]] = []       # every message the adapters actually read
        self.crash_after_submit = False                # accept the mail, then drop the connection
        self.rewrite_message_id = False                # a server that replaces our Message-ID
        self.refuse: str | None = None                 # "recipient" | "auth" | "disconnect"
        self.hide_sent = False                         # the Sent copy is not visible yet
        self.lax_header_search = False                 # a search that also matches unrelated mail
        self._uid = 100

    # --- SMTP ----------------------------------------------------------------------------------
    def smtp(self) -> "FakeSmtp":
        return FakeSmtp(self)

    def mailbox(self) -> "FakeMailbox":
        return FakeMailbox(self)

    def _store(self, folder: str, raw: bytes) -> Stored:
        self._uid += 1
        item = Stored(self._uid, raw)
        self.folders[folder].append(item)
        return item

    # --- helpers for tests ---------------------------------------------------------------------
    def deliver(self, *, message_id: str, from_addr: str, subject: str, body: str, to: str = "me@gmail.test",
                in_reply_to: str | None = None, references: tuple[str, ...] = (),
                delivered_to: str | None = None, date: str | None = None) -> int:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = from_addr, to, subject
        if delivered_to:
            msg["Delivered-To"] = delivered_to
        msg["Date"], msg["Message-ID"] = date or formatdate(), message_id
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        if references:
            msg["References"] = " ".join(references)
        msg.set_content(body)
        return self._store(INBOX, bytes(msg)).uid

    def sent_ids(self) -> list[str]:
        return [item.headers()["Message-ID"] for item in self.folders[SENT]]


class FakeSmtp:
    def __init__(self, server: FakeGmail) -> None:
        self._s = server

    def send(self, message: EmailMessage) -> None:
        if self._s.refuse == "auth":
            raise MailRejected("Gmail rejected the login (SMTP 535)")
        if self._s.refuse == "recipient":
            raise MailRejected("Gmail refused the message (SMTPRecipientsRefused)")
        if self._s.refuse == "disconnect":
            raise MailUnavailable("could not reach Gmail (SMTPServerDisconnected)")
        self._s.submitted.append(message)
        filed = message
        if self._s.rewrite_message_id:
            filed = EmailMessage()
            for name, value in message.items():
                if name.lower() != "message-id":
                    filed[name] = value
            filed["Message-ID"] = "<rewritten-by-server@mail.gmail.com>"
            filed.set_content(message.get_body(("plain",)).get_content())
        self._s._store(SENT, bytes(filed))
        if self._s.crash_after_submit:
            self._s.crash_after_submit = False
            raise MailUnavailable("the connection dropped after Gmail accepted the message")


class FakeMailbox:
    def __init__(self, server: FakeGmail) -> None:
        self._s = server
        self.searched_since: list = []

    def sent_uids(self, message_id: str, key_token: str) -> list[int]:
        if self._s.hide_sent:
            return []
        found = [i.uid for i in self._s.folders[SENT] if i.headers()["Message-ID"] == message_id]
        return found or [i.uid for i in self._s.folders[SENT] if i.headers()["X-Revos-Key"] == key_token]

    def has_sent(self, message_id: str, key_token: str) -> bool:
        return bool(self.sent_uids(message_id, key_token))

    def _pending(self, folder: str):
        return [i for i in self._s.folders[folder] if "revos-processed" not in i.labels]

    def uids_from(self, folder: str, address: str, since=None) -> list[int]:
        self.searched_since.append(since)
        return [i.uid for i in self._pending(folder)
                if address.lower() in (i.headers()["From"] or "").lower()]

    def uids_replying_to(self, folder: str, message_id: str) -> list[int]:
        out = []
        for item in self._pending(folder):
            head = item.headers()
            hay = f"{head['In-Reply-To'] or ''} {head['References'] or ''}"
            if re.search(re.escape(message_id), hay) or (self._s.lax_header_search and hay.strip()):
                out.append(item.uid)
        return out

    def uids_with_message_id(self, folder: str, message_id: str) -> list[int]:
        return [i.uid for i in self._s.folders[folder] if i.headers()["Message-ID"] == message_id]

    def fetch(self, folder: str, uid: int) -> bytes:
        self._s.fetched.append((folder, uid))
        return next(i.raw for i in self._s.folders[folder] if i.uid == uid)

    def add_label(self, folder: str, uid: int, label: str) -> None:
        next(i for i in self._s.folders[folder] if i.uid == uid).labels.add(label)
