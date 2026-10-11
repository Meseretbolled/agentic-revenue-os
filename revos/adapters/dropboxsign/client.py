"""The real Dropbox Sign adapter. TEST MODE ONLY, enforced in code.

What the docs (developers.hellosign.com) say, and what is used here:
* Auth: HTTP Basic with the API key as the username and an empty password.
* Send: POST https://api.hellosign.com/v3/signature_request/send with `test_mode`, `title`, `subject`,
  `message`, `signers`, `metadata` and the file. The docs describe a JSON body but a PDF has to travel as
  multipart/form-data; the multipart field names used here (`files[0]`, `signers[0][email_address]`,
  `metadata[...]`, `test_mode=1`) are the API's form encoding and are checked against the live service.
* Events: POST multipart/form-data with the event in a field named `json`; `event.event_hash` is
  HMAC-SHA256 (hex, as `openssl dgst` prints) over `event_time + event_type` keyed with the API key; the
  receiver answers HTTP 200 with a body containing "Hello API Event Received".

The key is held privately and never appears in an error or a repr."""

import hashlib
import hmac
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from revos.domain.errors import PermanentError, RevosError, WebhookRejected
from revos.ports.esign import SignatureEvent, SignatureRequest

BASE = "https://api.hellosign.com/v3"
RETRY_STATUSES = {429, 500, 502, 503, 504}
EVENT_KINDS = {"signature_request_sent": "sent", "signature_request_viewed": "viewed",
               "signature_request_signed": "signed", "signature_request_all_signed": "all_signed",
               "callback_test": "test"}
METADATA_KEY = "revos_key"


class TestModeViolation(PermanentError):
    """A request that is not in test mode (or a response that says it was not) is refused outright."""

    __test__ = False


class DropboxSignError(PermanentError):
    def __init__(self, what: str, code: str) -> None:
        self.code = code
        super().__init__(f"Dropbox Sign {what} failed: {code}")


class DropboxSignUnavailable(RevosError):
    def __init__(self, what: str, why: str) -> None:
        super().__init__(f"Dropbox Sign {what} unavailable: {why}")


def key_token(idempotency_key: str) -> str:
    """What goes in the request metadata: short, searchable, and not our raw key."""
    return "rv" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:30]


class DropboxSignAdapter:
    def __init__(self, api_key: str, *, http: httpx.Client | None = None, max_retries: int = 2,
                 sleep: Callable[[float], None] = time.sleep, timeout: float = 30.0) -> None:
        self._key = api_key
        self._http = http or httpx.Client(timeout=timeout)
        self._retries, self._sleep, self._timeout = max_retries, sleep, timeout

    def __repr__(self) -> str:
        return "DropboxSignAdapter(test_mode=True)"

    # --- plumbing ------------------------------------------------------------------------------------
    def _request(self, what: str, method: str, path: str, *, retry: bool = True,
                 **kwargs: Any) -> dict[str, Any]:
        reason = "network"
        attempts = self._retries + 1 if retry else 1
        for attempt in range(attempts):
            if attempt:
                self._sleep(min(10.0, 0.5 * 2 ** attempt))
            try:
                response = self._http.request(method, f"{BASE}{path}", auth=(self._key, ""),
                                              timeout=self._timeout, **kwargs)
            except httpx.HTTPError:
                reason = "network"
                continue
            if response.status_code in RETRY_STATUSES:
                reason = "rate_limited" if response.status_code == 429 else "server_error"
                continue
            try:
                body = response.json()
            except ValueError:
                body = {}
            if response.status_code >= 400:
                error = (body.get("error") or {}) if isinstance(body, dict) else {}
                raise DropboxSignError(what, str(error.get("error_name") or f"http_{response.status_code}"))
            return body if isinstance(body, dict) else {}
        raise DropboxSignUnavailable(what, reason)

    # --- the ESign port -------------------------------------------------------------------------------
    def send(self, request: SignatureRequest) -> str:
        if request.test_mode is not True:
            raise TestModeViolation("a signature request must be in test mode; this one is not")
        if not request.pdf:
            raise DropboxSignError("send", "no_document")
        data = {"test_mode": "1", "title": request.title[:200], "subject": request.title[:200],
                "message": "TEST MODE. A sample insertion order for a demonstration; not legally binding.",
                "signers[0][email_address]": request.signer_email, "signers[0][name]": request.signer_name,
                f"metadata[{METADATA_KEY}]": key_token(request.idempotency_key)}
        # no automatic retry: a timeout may mean the request WAS created (the caller checks before retrying)
        body = self._request("send", "POST", "/signature_request/send", retry=False, data=data,
                             files={"files[0]": (request.filename, request.pdf, "application/pdf")})
        created = body.get("signature_request") or {}
        sid = str(created.get("signature_request_id") or "")
        if not sid:
            raise DropboxSignError("send", "no_signature_request_id")
        if created.get("test_mode") is not True:               # the service says it is NOT a test: undo it
            try:
                self._request("cancel", "POST", f"/signature_request/cancel/{sid}", retry=False)
            except RevosError:
                pass
            raise TestModeViolation("Dropbox Sign did not confirm test mode for the request just created")
        return sid

    def find_by_key(self, idempotency_key: str) -> str | None:
        """The id of the request carrying our key in its metadata, if the search can see it. A newly created
        request may not be searchable immediately (documented), so None is NOT proof it was never created."""
        token = key_token(idempotency_key)
        page = 1
        while page <= 5:
            body = self._request("search", "GET", "/signature_request/list",
                                 params={"query": f"metadata:{token}", "page": page, "page_size": 100})
            for item in body.get("signature_requests") or []:
                if (item.get("metadata") or {}).get(METADATA_KEY) == token:
                    return str(item["signature_request_id"])
            info = body.get("list_info") or {}
            if page >= int(info.get("num_pages") or 1):
                break
            page += 1
        return None

    def get(self, envelope_id: str) -> dict[str, Any]:
        return (self._request("get", "GET", f"/signature_request/{envelope_id}").get("signature_request")
                or {})

    def status(self, envelope_id: str) -> Literal["awaiting", "signed"]:
        return "signed" if self.get(envelope_id).get("is_complete") is True else "awaiting"

    def parse_event(self, payload: Mapping[str, Any]) -> SignatureEvent:
        """Verify the event hash exactly as documented and read the event. Raises WebhookRejected."""
        try:
            event = payload["event"]
            kind, when, given = str(event["event_type"]), str(event["event_time"]), str(event["event_hash"])
        except (KeyError, TypeError) as exc:
            raise WebhookRejected("the webhook body is not in the expected shape") from exc
        expected = hmac.new(self._key.encode(), (when + kind).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(given.lower(), expected):
            raise WebhookRejected("the webhook signature does not verify")
        request = payload.get("signature_request") or {}
        envelope = str(request.get("signature_request_id") or "")
        if not envelope and kind != "callback_test":
            raise WebhookRejected("the webhook names no signature request")
        try:
            occurred = datetime.fromtimestamp(int(when), UTC)
        except ValueError as exc:
            raise WebhookRejected("the webhook has no usable event_time") from exc
        return SignatureEvent(f"{envelope}:{kind}:{when}", EVENT_KINDS.get(kind, "other"), envelope,
                              occurred, kind)
