from collections.abc import Mapping
from typing import Any, Literal

from revos.ports.esign import SignatureRequest


class MockESign:
    """Test mode only, like the Dropbox Sign account: a live request is refused."""

    def __init__(self) -> None:
        self.requests: list[SignatureRequest] = []
        self._by_key: dict[str, str] = {}
        self._signed: set[str] = set()

    def send(self, request: SignatureRequest) -> str:
        if not request.test_mode:
            raise ValueError("e-signature is limited to test mode")
        self.requests.append(request)
        envelope = f"env-{len(self._by_key) + 1}"
        self._by_key.setdefault(request.idempotency_key, envelope)
        return self._by_key[request.idempotency_key]

    def find_by_key(self, idempotency_key: str) -> str | None:
        return self._by_key.get(idempotency_key)

    def status(self, envelope_id: str) -> Literal["awaiting", "signed"]:
        return "signed" if envelope_id in self._signed else "awaiting"

    def simulate_signed(self, envelope_id: str) -> None:
        self._signed.add(envelope_id)

    # --- ActionProvider ---
    def find(self, idempotency_key: str) -> str | None:
        return self.find_by_key(idempotency_key)

    def perform(self, action: Mapping[str, Any]) -> str:
        if action["type"] != "send_io":
            raise ValueError(f"e-sign cannot perform {action['type']!r}")
        payload = action["payload"]
        if not action.get("to_email"):
            raise ValueError("no signer: the contact row has no email")
        return self.send(SignatureRequest(action["idempotency_key"], payload["title"],
                                          payload["document"], action["to_email"],
                                          payload["signer_name"], test_mode=True))
