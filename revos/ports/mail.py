import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


def message_id_for(idempotency_key: str) -> str:
    """Deterministic Message-ID, so a sent mail can be found again from our key alone."""
    return f"<{hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]}@revos.example>"


@dataclass(frozen=True)
class OutboundEmail:
    idempotency_key: str
    to: str
    subject: str
    body: str
    in_reply_to: str | None = None


@dataclass(frozen=True)
class InboundEmail:
    message_id: str
    from_addr: str
    to: str
    subject: str
    body: str
    received_at: datetime
    in_reply_to: str | None = None
    is_html: bool = False


class Mailer(Protocol):
    def send(self, email: OutboundEmail) -> str:
        """Send and return the Message-ID."""
        ...

    def find_sent(self, idempotency_key: str) -> str | None:
        """Message-ID of an email already sent for this key, if any."""
        ...


class Inbox(Protocol):
    def poll(self) -> list[InboundEmail]:
        """New messages not yet marked processed."""
        ...

    def mark_processed(self, message_id: str) -> None: ...
