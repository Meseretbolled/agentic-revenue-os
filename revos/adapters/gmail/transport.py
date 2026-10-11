"""The two wires to Gmail, each behind a small interface so tests can use a fake.

SMTP sends. IMAP looks things up and fetches only what it is told to. Neither ever puts the app
password in an error, a log line or a repr."""

import imaplib
import smtplib
from datetime import date
from email.message import EmailMessage
from typing import Protocol

from revos.domain.errors import PermanentError, RevosError

SENT = '"[Gmail]/Sent Mail"'
INBOX = "INBOX"
PROCESSED_LABEL = "revos-processed"
TIMEOUT = 20.0


class MailRejected(PermanentError):
    """Gmail refused the message or the login for good. Retrying cannot help."""


class MailUnavailable(RevosError):
    """Gmail could not be reached, or dropped the connection. Safe to retry after a lookup."""


class SmtpTransport(Protocol):
    def send(self, message: EmailMessage) -> None: ...


class Mailbox(Protocol):
    """The few IMAP questions we ask. Each returns UIDs or one message, never a listing."""

    def sent_uids(self, message_id: str, key_token: str) -> list[int]:
        """The Sent copies of one email: found by the Message-ID we set, else by X-Revos-Key."""
        ...

    def has_sent(self, message_id: str, key_token: str) -> bool:
        """Is a message with this Message-ID (or our key header) in the Sent mailbox?"""
        ...

    def uids_from(self, folder: str, address: str, since: date | None = None) -> list[int]:
        """Unprocessed messages in `folder` from exactly this address, from `since` (a day) on."""
        ...

    def uids_replying_to(self, folder: str, message_id: str) -> list[int]:
        """Unprocessed messages whose In-Reply-To or References name this Message-ID."""
        ...

    def uids_with_message_id(self, folder: str, message_id: str) -> list[int]: ...

    def fetch(self, folder: str, uid: int) -> bytes: ...

    def add_label(self, folder: str, uid: int, label: str) -> None: ...


class SmtplibTransport:
    def __init__(self, address: str, app_password: str, *, host: str = "smtp.gmail.com", port: int = 465,
                 smtp_class: type = smtplib.SMTP_SSL) -> None:
        self._address, self._password = address, app_password
        self._host, self._port, self._cls = host, port, smtp_class

    def __repr__(self) -> str:
        return f"SmtplibTransport(host={self._host!r})"

    def send(self, message: EmailMessage) -> None:
        try:
            with self._cls(self._host, self._port, timeout=TIMEOUT) as smtp:
                smtp.login(self._address, self._password)
                smtp.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            raise MailRejected(f"Gmail rejected the login (SMTP {exc.smtp_code})") from None
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
            code = getattr(exc, "smtp_code", None)
            if code is not None and 400 <= code < 500:
                raise MailUnavailable(f"Gmail deferred the message (SMTP {code})") from None
            raise MailRejected(f"Gmail refused the message ({type(exc).__name__})") from None
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, TimeoutError, OSError) as exc:
            raise MailUnavailable(f"could not reach Gmail ({type(exc).__name__})") from None
        except smtplib.SMTPException as exc:
            raise MailUnavailable(f"Gmail SMTP error ({type(exc).__name__})") from None


class ImapMailbox:
    """Gmail over IMAP. Uses Gmail's X-GM-RAW search and X-GM-LABELS; these are Gmail-specific
    and have only been exercised by the live tests."""

    def __init__(self, address: str, app_password: str, *, host: str = "imap.gmail.com",
                 imap_class: type = imaplib.IMAP4_SSL) -> None:
        self._address, self._password, self._host, self._cls = address, app_password, host, imap_class

    def __repr__(self) -> str:
        return f"ImapMailbox(host={self._host!r})"

    def _session(self, folder: str, readonly: bool = True):
        try:
            client = self._cls(self._host, 993, timeout=TIMEOUT)
            client.login(self._address, self._password)
            status, _ = client.select(folder, readonly=readonly)
            if status != "OK":
                raise MailUnavailable("could not open the mailbox")
            return client
        except imaplib.IMAP4.error:
            raise MailRejected("Gmail rejected the IMAP login") from None
        except (OSError, TimeoutError):
            raise MailUnavailable("could not reach Gmail over IMAP") from None

    @staticmethod
    def _uids(client, *criteria: str) -> list[int]:
        status, data = client.uid("SEARCH", None, *criteria)
        if status != "OK":
            raise MailUnavailable("the IMAP search failed")
        return [int(x) for x in (data[0] or b"").split()]

    @staticmethod
    def _close(client) -> None:
        try:
            client.logout()
        except (imaplib.IMAP4.error, OSError):
            pass

    def sent_uids(self, message_id: str, key_token: str) -> list[int]:
        client = self._session(SENT)
        try:
            return (self._uids(client, "X-GM-RAW", f'"rfc822msgid:{message_id}"')
                    or self._uids(client, "HEADER", "X-Revos-Key", key_token))
        finally:
            self._close(client)

    def has_sent(self, message_id: str, key_token: str) -> bool:
        return bool(self.sent_uids(message_id, key_token))

    def uids_from(self, folder: str, address: str, since: date | None = None) -> list[int]:
        client = self._session(folder)
        try:
            after = f" after:{since:%Y/%m/%d}" if since else ""
            return self._uids(client, "X-GM-RAW", f'"from:{address} -label:{PROCESSED_LABEL}{after}"')
        finally:
            self._close(client)

    def uids_replying_to(self, folder: str, message_id: str) -> list[int]:
        client = self._session(folder)
        try:
            found: set[int] = set()
            for header in ("In-Reply-To", "References"):
                found |= set(self._uids(client, "X-GM-RAW", f'"-label:{PROCESSED_LABEL}"',
                                        "HEADER", header, f'"{message_id}"'))
            return sorted(found)
        finally:
            self._close(client)

    def uids_with_message_id(self, folder: str, message_id: str) -> list[int]:
        client = self._session(folder)
        try:
            return self._uids(client, "X-GM-RAW", f'"rfc822msgid:{message_id}"')
        finally:
            self._close(client)

    def fetch(self, folder: str, uid: int) -> bytes:
        client = self._session(folder)
        try:
            status, data = client.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if status != "OK" or not data or not isinstance(data[0], tuple):
                raise MailUnavailable("the IMAP fetch failed")
            return bytes(data[0][1])
        finally:
            self._close(client)

    def add_label(self, folder: str, uid: int, label: str) -> None:
        client = self._session(folder, readonly=False)
        try:
            status, _ = client.uid("STORE", str(uid), "+X-GM-LABELS", f"({label})")
            if status != "OK":
                raise MailUnavailable("could not mark the message as processed")
        finally:
            self._close(client)
