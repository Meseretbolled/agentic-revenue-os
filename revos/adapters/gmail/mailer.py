"""Gmail SMTP behind the Mailer port, and as an outbox provider.

* The recipient is whatever the outbox read from the CONTACT ROW; a recipient in the payload is
  ignored. It must also be on the allowlist (the prospect inbox and the sender in demo mode).
* The email must carry the unsubscribe line and the postal address, or it is not sent.
* The Message-ID is derived from the action's idempotency key, so find_sent(key) can ask the Sent
  mailbox whether this exact email already left, and a retry after a crash never sends twice."""

from collections.abc import Mapping, Sequence
from typing import Any

from revos.adapters.gmail.mime import build_message, key_token
from revos.adapters.gmail.transport import Mailbox, MailRejected, SmtpTransport
from revos.ports.mail import OutboundEmail, build_ics, message_id_for

_MAIL_TYPES = {"first_touch", "follow_up"}


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
            references=email.references, attachments=email.attachments, sender_name=self._sender_name)
        self._smtp.send(message)
        return message_id

    def find_sent(self, idempotency_key: str) -> str | None:
        message_id = message_id_for(idempotency_key)
        return message_id if self._mailbox.has_sent(message_id, key_token(idempotency_key)) else None

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
            attachments, tuple(payload.get("references", ()))))
