"""Building the message we send, and reading a message we receive."""

import hashlib
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, getaddresses, parseaddr, parsedate_to_datetime

from revos.ports.mail import Attachment, InboundEmail

_ID = re.compile(r"<[^<>\s]+>")


def key_token(idempotency_key: str) -> str:
    """The same hash message_id_for uses; kept in an X-Revos-Key header as a second way to find a
    sent message if Gmail ever rewrites the Message-ID."""
    return hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]


def build_message(*, sender: str, to: str, subject: str, body: str, message_id: str, token: str,
                  in_reply_to: str | None = None, references: Sequence[str] = (),
                  attachments: Sequence[Attachment] = (), sender_name: str | None = None,
                  reply_to: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"{sender_name} <{sender}>" if sender_name else sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = message_id
    msg["X-Revos-Key"] = token
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["List-Unsubscribe"] = f"<mailto:{sender}?subject=unsubscribe>"
    if in_reply_to:
        chain = [r for r in references if r != in_reply_to] + [in_reply_to]
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = " ".join(chain)
    msg.set_content(body)
    for item in attachments:
        main, _, rest = item.content_type.partition("/")
        subtype, _, params = rest.partition(";")
        extra = dict(p.strip().split("=", 1) for p in params.split(";") if "=" in p)
        if main == "text":
            msg.add_attachment(item.content, subtype=subtype.strip(), filename=item.filename,
                               params=extra)
    return msg


def message_ids(value: str | None) -> tuple[str, ...]:
    return tuple(_ID.findall(value or ""))


def parse_message(raw: bytes) -> InboundEmail:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    body_part = msg.get_body(preferencelist=("plain", "html"))
    is_html = bool(body_part and body_part.get_content_type() == "text/html")
    body = body_part.get_content() if body_part else ""
    try:
        received = parsedate_to_datetime(msg["Date"]) if msg["Date"] else datetime.now(UTC)
    except (TypeError, ValueError):
        received = datetime.now(UTC)
    ids = message_ids(msg["Message-ID"])
    reply_to = message_ids(msg["In-Reply-To"])
    return InboundEmail(
        message_id=ids[0] if ids else "", from_addr=parseaddr(msg["From"] or "")[1].lower(),
        to=parseaddr(msg["To"] or "")[1].lower(), subject=str(msg["Subject"] or ""), body=body,
        received_at=received, in_reply_to=reply_to[0] if reply_to else None, is_html=is_html,
        references=message_ids(msg["References"]),
        to_addrs=tuple(dict.fromkeys(a.lower() for _, a in getaddresses(
            [str(msg[h]) for h in ("To", "Cc", "Delivered-To") if msg[h]]) if a)))
