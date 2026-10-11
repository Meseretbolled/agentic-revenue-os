"""Gmail SMTP behind the Mailer port, and as an outbox provider.

* The recipient is whatever the outbox read from the CONTACT ROW; a recipient in the payload is
  ignored. It must also be on the allowlist (the prospect inbox and the sender in demo mode).
* The email must carry the unsubscribe line and the postal address, or it is not sent.
* The Message-ID is derived from the action's idempotency key, so find_sent(key) can ask the Sent
  mailbox whether this exact email already left, and a retry after a crash never sends twice."""

import re
from collections.abc import Mapping, Sequence
from typing import Any

from revos.adapters.gmail.mime import build_message, key_token, message_ids
from revos.adapters.gmail.transport import SENT, Mailbox, MailRejected, SmtpTransport
from revos.ports.mail import OutboundEmail, build_ics, message_id_for

_MAIL_TYPES = {"first_touch", "follow_up"}
_TAG = re.compile(r"[0-9a-f]{4,16}")


def plus_address(address: str, tag: str | None) -> str | None:
    """name@gmail.com + tag -> name+tag@gmail.com. Replies to it land in the same inbox, and the tag
    says which deal they are about. Not a recipient: it only goes in Reply-To."""
    if not tag or not _TAG.fullmatch(tag) or "@" not in address:
        return None
    local, _, domain = address.partition("@")
    return f"{local.split('+')[0]}+{tag}@{domain}"


class GmailMailer:
    def __init__(self, address: str, *, smtp: SmtpTransport, mailbox: Mailbox,
                 allowlist: frozenset[str] | None, required_footer: Sequence[str],
                 sender_name: str | None = None) -> None:
        if not all(s.strip() for s in required_footer):
            raise ValueError("the unsubscribe line and postal address are both required for real email")
        self._address = address
        self._smtp, self._mailbox = smtp, mailbox
        self._allow = None if allowlist is None else frozenset(a.strip().lower() for a in allowlist)
        self._footer = tuple(required_footer)
        self._sender_name = sender_name

    def __repr__(self) -> str:
        return "GmailMailer()"

    def send(self, email: OutboundEmail) -> str:
        to = email.to.strip().lower()
        if self._allow is not None and to not in self._allow:
            raise MailRejected("the recipient is not on the allowlist")
        missing = [f for f in self._footer if f not in email.body]
        if missing:
            raise MailRejected("the email lacks the required unsubscribe line or postal address")
        message_id = message_id_for(email.idempotency_key)
        message = build_message(
            sender=self._address, to=to, subject=email.subject, body=email.body, message_id=message_id,
            token=key_token(email.idempotency_key), in_reply_to=email.in_reply_to,
            references=email.references, attachments=email.attachments, sender_name=self._sender_name,
            reply_to=plus_address(self._address, email.deal_tag))
        self._smtp.send(message)
        return message_id

    def find_sent(self, idempotency_key: str) -> str | None:
        message_id = message_id_for(idempotency_key)
        return message_id if self._mailbox.has_sent(message_id, key_token(idempotency_key)) else None

    def actual_message_id(self, idempotency_key: str) -> str | None:
        """The Message-ID Gmail really gave the Sent copy (normally ours, but Gmail may rewrite
        it). None if the copy is not visible yet; the caller tries again later."""
        for uid in self._mailbox.sent_uids(message_id_for(idempotency_key), key_token(idempotency_key)):
            ids = message_ids(_header(self._mailbox.fetch(SENT, uid), "Message-ID"))
            if ids:
                return ids[0]
        return None

    # --- ActionProvider -------------------------------------------------------------------------
    def find(self, idempotency_key: str) -> str | None:
        return self.find_sent(idempotency_key)

    def perform(self, action: Mapping[str, Any]) -> str:
        if action["type"] not in _MAIL_TYPES:
            raise MailRejected(f"the mailer cannot perform {action['type']!r}")
        to = action.get("to_email")
        if not to:
            raise MailRejected("no recipient: the contact row has no email")
        payload = action["payload"]
        invite = payload.get("invite")
        attachments = ()
        if invite:
            from datetime import datetime
            attachments = (build_ics(action["idempotency_key"], datetime.fromisoformat(invite["start"]),
                                     int(invite.get("minutes", 30)), invite["summary"], to,
                                     organizer=self._address),)
        return self.send(OutboundEmail(
            action["idempotency_key"], to, payload["subject"], payload["body"], payload.get("in_reply_to"),
            attachments, tuple(payload.get("references", ())), payload.get("deal_tag")))


def _header(raw: bytes, name: str) -> str:
    from email import policy
    from email.parser import BytesParser
    return str(BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)[name] or "")
