from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class SignatureRequest:
    idempotency_key: str
    title: str
    document: str
    signer_email: str
    signer_name: str
    test_mode: bool = True       # Dropbox Sign stays in test mode; a request with it off is refused
    pdf: bytes | None = None     # the rendered IO, the only attachment
    filename: str = "insertion-order.pdf"


@dataclass(frozen=True)
class SignatureEvent:
    event_id: str                # stable across redeliveries of the same event
    kind: Literal["sent", "viewed", "signed", "all_signed", "test", "other"]
    envelope_id: str
    occurred_at: datetime
    event_type: str = ""


class ESign(Protocol):
    def send(self, request: SignatureRequest) -> str:
        """Send for signature and return the envelope id."""
        ...

    def find_by_key(self, idempotency_key: str) -> str | None: ...

    def status(self, envelope_id: str) -> Literal["awaiting", "signed"]: ...

    def parse_event(self, payload: Mapping[str, Any]) -> SignatureEvent:
        """Verify a webhook payload and read it. Raises WebhookRejected if it does not verify."""
        ...
