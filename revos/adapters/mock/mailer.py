from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from revos.adapters.mock.fixtures import load
from revos.ports.mail import InboundEmail, OutboundEmail, build_ics, message_id_for

_MAIL_TYPES = {"first_touch", "follow_up"}


class MockMailer:
    """Records every send call, including duplicates, so tests can see a double send.

    Also an ActionProvider for the outbox. The recipient comes from `to_email`, which the outbox
    reads from the contact row; a recipient written into the action payload is ignored.
    """

    def __init__(self, crash_after_send: bool = False) -> None:
        self.crash_after_send = crash_after_send    # accepted, then the connection "drops"
        self.sent: list[OutboundEmail] = []
        self._by_key: dict[str, str] = {}

    def send(self, email: OutboundEmail) -> str:
        self.sent.append(email)
        message_id = message_id_for(email.idempotency_key)
        self._by_key[email.idempotency_key] = message_id
        if self.crash_after_send:
            self.crash_after_send = False
            raise ConnectionError("connection lost after the mail server accepted the message")
        return message_id

    def find_sent(self, idempotency_key: str) -> str | None:
        return self._by_key.get(idempotency_key)

    # --- ActionProvider ---
    def find(self, idempotency_key: str) -> str | None:
        return self.find_sent(idempotency_key)

    def perform(self, action: Mapping[str, Any]) -> str:
        if action["type"] not in _MAIL_TYPES:
            raise ValueError(f"mailer cannot perform {action['type']!r}")
        to = action.get("to_email")
        if not to:
            raise ValueError("no recipient: the contact row has no email")
        payload = action["payload"]
        invite = payload.get("invite")
        attachments = ()
        if invite:                       # the attendee is the contact-row address, nothing else
            attachments = (build_ics(action["idempotency_key"],
                                     datetime.fromisoformat(invite["start"]),
                                     int(invite.get("minutes", 30)), invite["summary"], to),)
        return self.send(OutboundEmail(action["idempotency_key"], to, payload["subject"],
                                       payload["body"], payload.get("in_reply_to"), attachments,
                                       tuple(payload.get("references", ()))))


class MockInbox:
    def __init__(self) -> None:
        self._messages: dict[str, InboundEmail] = {}
        self._processed: set[str] = set()
        self.polls = 0

    def deliver(self, email: InboundEmail) -> None:
        self._messages.setdefault(email.message_id, email)

    def deliver_fixture(self, name_fragment: str, to: str, now: datetime | None = None) -> InboundEmail:
        for item in load("inbound_emails")["emails"]:
            if name_fragment in item["message_id"]:
                email = InboundEmail(item["message_id"], item["from_addr"], to, item["subject"],
                                     item["body"], now or datetime.now(UTC))
                self.deliver(email)
                return email
        raise KeyError(name_fragment)

    def poll(self) -> list[InboundEmail]:
        self.polls += 1
        return [m for mid, m in self._messages.items() if mid not in self._processed]

    def mark_processed(self, message_id: str) -> None:
        self._processed.add(message_id)
