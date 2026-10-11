"""The Dropbox Sign callback: verify, deduplicate, queue. Nothing is trusted before the hash verifies.

Dropbox Sign posts multipart/form-data with the event JSON in a field named `json`, and requires the
reply "Hello API Event Received" with status 200 or it treats the delivery as failed and resends it."""

import json
from collections.abc import Callable, Mapping
from typing import Any

import psycopg

from revos.domain.errors import WebhookRejected
from revos.obs import log
from revos.policy.audit import write_audit
from revos.ports.clock import Clock
from revos.ports.esign import ESign
from revos.runtime.events import record_event

HELLO = "Hello API Event Received"


def parse_callback_body(fields: Mapping[str, Any]) -> dict[str, Any] | None:
    """The event JSON from the form field `json` (or a bare JSON object), or None if unreadable."""
    raw = fields.get("json")
    try:
        data = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class SignatureWebhook:
    def __init__(self, esign: ESign, conn_factory: Callable[[], psycopg.Connection], clock: Clock) -> None:
        self._esign, self._connect, self._clock = esign, conn_factory, clock

    def handle(self, payload: Mapping[str, Any]) -> str:
        """'test', 'queued', 'duplicate' or 'rejected'. A rejection is audited without the body."""
        with self._connect() as conn:
            conn.autocommit = True
            try:
                event = self._esign.parse_event(payload)
            except WebhookRejected as exc:
                write_audit(conn, "webhook", "webhook.rejected", None, {"why": str(exc)})
                log("esign", "webhook rejected", level="warning", why=str(exc))
                return "rejected"
            if event.kind == "test":
                return "test"
            created = record_event(conn, f"signature:{event.event_id}", "signature.event",
                                   {"envelope_id": event.envelope_id, "kind": event.kind,
                                    "event_type": event.event_type}, self._clock)
            return "queued" if created is not None else "duplicate"
