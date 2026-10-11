import hashlib
import hmac
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from revos.domain.errors import WebhookRejected
from revos.ports.esign import SignatureEvent, SignatureRequest


class MockESign:
    """Test mode only, like the Dropbox Sign account: a live request is refused."""

    def __init__(self, secret: str = "mock-esign-secret") -> None:
        self._secret = secret.encode()
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
        # the address is the lead record's signer_email, else the contact row's: never the payload
        recipient = action.get("signer_email") or action.get("to_email")
        if not recipient:
            raise ValueError("no signer: neither the lead record nor the contact row has an email")
        return self.send(SignatureRequest(action["idempotency_key"], payload["title"],
                                          payload["document"], recipient,
                                          payload["signer_name"], test_mode=True))

    # --- webhooks, shaped like Dropbox Sign's: event_hash = HMAC-SHA256(key, event_time + type) ---
    def _hash(self, event_time: str, event_type: str) -> str:
        return hmac.new(self._secret, f"{event_time}{event_type}".encode(), hashlib.sha256).hexdigest()

    def signed_event(self, envelope_id: str, *, event_type: str = "signature_request_all_signed",
                     event_time: str | None = None) -> dict[str, Any]:
        """A webhook body as the provider would send it (test helper)."""
        when = event_time or str(int(datetime(2026, 10, 20, 12, tzinfo=UTC).timestamp()))
        return {"event": {"event_type": event_type, "event_time": when,
                          "event_hash": self._hash(when, event_type)},
                "signature_request": {"signature_request_id": envelope_id}}

    def parse_event(self, payload: Mapping[str, Any]) -> SignatureEvent:
        try:
            event = payload["event"]
            kind, when, given = event["event_type"], str(event["event_time"]), str(event["event_hash"])
            envelope = payload["signature_request"]["signature_request_id"]
        except (KeyError, TypeError) as exc:
            raise WebhookRejected("the webhook body is not in the expected shape") from exc
        if not hmac.compare_digest(given, self._hash(when, kind)):
            raise WebhookRejected("the webhook signature does not verify")
        return SignatureEvent(f"{envelope}:{kind}:{when}",
                              "all_signed" if kind == "signature_request_all_signed" else "other",
                              str(envelope), datetime.fromtimestamp(int(when), UTC))
