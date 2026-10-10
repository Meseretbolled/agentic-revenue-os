from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class SignatureRequest:
    idempotency_key: str
    title: str
    document: str
    signer_email: str
    signer_name: str
    test_mode: bool = True       # Dropbox Sign stays in test mode


class ESign(Protocol):
    def send(self, request: SignatureRequest) -> str:
        """Send for signature and return the envelope id."""
        ...

    def find_by_key(self, idempotency_key: str) -> str | None: ...

    def status(self, envelope_id: str) -> Literal["awaiting", "signed"]: ...
