"""Sends an IO for signature as an outbox action, safely.

The outbox calls `find(key)` before every attempt and `perform(action)` only when nothing is found. This
wrapper adds what an e-signature request needs on top of that:

* the attempt is recorded on its OWN connection before the create call, so a crash or a lost response
  leaves a trace that survives the outbox's rolled-back transaction;
* before a retry the provider is asked about our key (by id if we know it, else by the metadata we
  attached); if it cannot be told whether a request was created, the action goes to REVIEW and nothing is
  sent again;
* the document is rendered to a PDF here, the recipient is the lead record's signer address (never a
  payload), and the request is always test mode."""

from collections.abc import Callable, Mapping
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from revos.adapters.dropboxsign.client import DropboxSignError, TestModeViolation
from revos.domain.errors import PermanentError
from revos.obs import log
from revos.ports.esign import ESign, SignatureRequest
from revos.skills.io_pdf import render_io_pdf


class ReviewRequired(PermanentError):
    """We cannot tell whether the request was created. A person must look; it is NOT sent again."""


class ESignOutboxProvider:
    def __init__(self, esign: ESign, conn_factory: Callable[[], psycopg.Connection]) -> None:
        self._esign, self._connect = esign, conn_factory

    # --- our own record of the attempt (separate connection, autocommit) ----------------------------------
    def _attempt(self, key: str) -> tuple[str, str | None] | None:
        with self._connect() as conn:
            conn.autocommit = True
            row = conn.execute("SELECT state, signature_request_id FROM esign_attempts "
                               "WHERE idempotency_key = %s", (key,)).fetchone()
        return (row[0], row[1]) if row else None

    def _record(self, key: str, state: str, sid: str | None = None) -> None:
        with self._connect() as conn:
            conn.autocommit = True
            conn.execute(
                "INSERT INTO esign_attempts (idempotency_key, state, signature_request_id) "
                "VALUES (%s, %s, %s) ON CONFLICT (idempotency_key) DO UPDATE SET state = EXCLUDED.state, "
                "signature_request_id = coalesce(EXCLUDED.signature_request_id, "
                "esign_attempts.signature_request_id), updated_at = now()", (key, state, sid))

    def _forget(self, key: str) -> None:
        with self._connect() as conn:
            conn.autocommit = True
            conn.execute("DELETE FROM esign_attempts WHERE idempotency_key = %s", (key,))

    # --- ActionProvider ---------------------------------------------------------------------------------
    def find(self, idempotency_key: str) -> str | None:
        known = self._attempt(idempotency_key)
        if known and known[0] == "created" and known[1]:
            return known[1]
        if known is None:
            return None                                         # never attempted: perform may send
        found = self._esign.find_by_key(idempotency_key)        # an attempt exists: ask the provider
        if found:
            self._record(idempotency_key, "created", found)
            return found
        return None                                             # perform decides (it will send nothing)

    def perform(self, action: Mapping[str, Any]) -> str:
        key = action["idempotency_key"]
        if self._attempt(key) is not None:
            # an earlier attempt exists and the provider does not show a request for it: we cannot tell
            # whether it was created (a new request may not be searchable yet). Never send a second one.
            self._record(key, "review")
            self._audit(action, "esign.review_required")
            raise ReviewRequired("an earlier attempt to send this IO exists but Dropbox Sign shows no "
                                 "request for it: not sending again, a person must check the account")
        recipient = action.get("signer_email") or action.get("to_email")
        if not recipient:
            raise PermanentError("no signer: neither the lead record nor the contact row has an email")
        payload = action["payload"]
        request = SignatureRequest(
            key, payload["title"], payload["document"], recipient, payload["signer_name"], test_mode=True,
            pdf=render_io_pdf(payload["document"]))
        self._record(key, "sending")
        try:
            sid = self._esign.send(request)
        except TestModeViolation:
            self._record(key, "review")                  # it may have been created before we noticed
            raise
        except DropboxSignError:
            self._forget(key)                    # a clear refusal: nothing was created, a retry is safe
            raise
        # any other failure (a timeout, a dropped connection) leaves the attempt as 'sending': ambiguous
        self._record(key, "created", sid)
        log("esign", "signature request created (test mode)", signature_request_id=sid)
        return sid

    def _audit(self, action: Mapping[str, Any], event: str) -> None:
        with self._connect() as conn:
            conn.autocommit = True
            detail = {"action_id": str(action.get("id")), "idempotency_key": action["idempotency_key"]}
            conn.execute("INSERT INTO audit_log (actor, event, deal_id, detail) VALUES ('esign', %s, %s, %s)",
                         (event, action.get("deal_id"), Jsonb(detail)))
