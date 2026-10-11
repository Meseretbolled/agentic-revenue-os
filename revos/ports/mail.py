import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol


def message_id_for(idempotency_key: str) -> str:
    """Deterministic Message-ID, so a sent mail can be found again from our key alone."""
    return f"<{hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]}@revos.example>"


@dataclass(frozen=True)
class Attachment:
    filename: str
    content_type: str
    content: str


@dataclass(frozen=True)
class OutboundEmail:
    idempotency_key: str
    to: str
    subject: str
    body: str
    in_reply_to: str | None = None
    attachments: tuple[Attachment, ...] = ()
    references: tuple[str, ...] = ()           # the whole thread so far, oldest first
    deal_tag: str | None = None                # short deal id, carried in the Reply-To plus address


def build_ics(uid: str, start: datetime, minutes: int, summary: str, attendee: str,
              organizer: str = "meetings@revos.example") -> Attachment:
    """A calendar invite. The attendee is the address the email is going to, supplied by the
    caller from the contact row, never taken from anything a prospect wrote."""
    def stamp(moment: datetime) -> str:
        return moment.strftime("%Y%m%dT%H%M%SZ")

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//revos//meeting//EN", "METHOD:REQUEST",
             "BEGIN:VEVENT", f"UID:{uid}@revos.example", f"DTSTAMP:{stamp(start)}",
             f"DTSTART:{stamp(start)}", f"DTEND:{stamp(start + timedelta(minutes=minutes))}",
             f"SUMMARY:{summary}", f"ORGANIZER:mailto:{organizer}",
             f"ATTENDEE;RSVP=TRUE:mailto:{attendee}", "END:VEVENT", "END:VCALENDAR"]
    return Attachment("invite.ics", "text/calendar; method=REQUEST", "\r\n".join(lines) + "\r\n")


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
    references: tuple[str, ...] = ()
    to_addrs: tuple[str, ...] = ()             # every address it was sent to (To, Cc, Delivered-To)


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
