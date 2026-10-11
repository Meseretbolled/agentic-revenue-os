"""The real Fireflies adapter: GraphQL at https://api.fireflies.ai/graphql with a Bearer key.

It lists recent transcripts (id, title, date) and fetches one (participants, speaker-labelled
sentences, Fireflies' own summary). Retries 429 and 5xx with backoff; any other client error is
permanent. The key is held privately and never appears in an error or a repr; errors carry
Fireflies' error code and nothing else from the response."""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from revos.domain.errors import PermanentError, RevosError
from revos.ports.transcripts import Sentence, Transcript, TranscriptInfo

ENDPOINT = "https://api.fireflies.ai/graphql"
LIST_QUERY = "query Recent($limit: Int) { transcripts(limit: $limit) { id title date dateString } }"
GET_QUERY = ("query One($id: String!) { transcript(id: $id) { id title date dateString participants "
             "sentences { index speaker_name text start_time end_time } "
             "summary { overview action_items } } }")
RETRY_STATUSES = {429, 500, 502, 503, 504}
TRANSIENT_CODES = {"too_many_requests", "rate_limited", "internal_error", "server_error"}


class FirefliesError(PermanentError):
    def __init__(self, what: str, code: str) -> None:
        self.code = code
        super().__init__(f"Fireflies {what} failed: {code}")


class FirefliesUnavailable(RevosError):
    def __init__(self, what: str, why: str) -> None:
        super().__init__(f"Fireflies {what} unavailable: {why}")


def parse_date(item: dict[str, Any]) -> datetime:
    """Fireflies gives `dateString` (ISO) and `date` (epoch milliseconds)."""
    raw = item.get("dateString")
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            pass
    millis = item.get("date")
    if isinstance(millis, int | float) and not isinstance(millis, bool):
        return datetime.fromtimestamp(millis / 1000, UTC)
    return datetime.fromtimestamp(0, UTC)


class FirefliesAdapter:
    def __init__(self, api_key: str, *, http: httpx.Client | None = None, max_retries: int = 3,
                 sleep: Callable[[float], None] = time.sleep, timeout: float = 20.0,
                 list_limit: int = 25) -> None:
        self._key = api_key
        self._http = http or httpx.Client(timeout=timeout)
        self._retries, self._sleep, self._timeout, self._limit = max_retries, sleep, timeout, list_limit

    def __repr__(self) -> str:
        return "FirefliesAdapter()"

    def _query(self, what: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        reason = "network"
        for attempt in range(self._retries + 1):
            if attempt:
                self._sleep(min(30.0, 0.5 * 2 ** attempt))
            try:
                response = self._http.post(
                    ENDPOINT, json={"query": query, "variables": variables}, timeout=self._timeout,
                    headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"})
            except httpx.HTTPError:
                reason = "network"
                continue
            if response.status_code in RETRY_STATUSES:
                reason = "rate_limited" if response.status_code == 429 else "server_error"
                retry = response.headers.get("Retry-After", "")
                if retry.isdigit():
                    self._sleep(min(30.0, float(retry)))
                continue
            try:
                body = response.json()
            except ValueError:
                body = {}
            errors = body.get("errors") if isinstance(body, dict) else None
            if errors:
                first = errors[0] if isinstance(errors[0], dict) else {}
                code = str(first.get("code") or (first.get("extensions") or {}).get("code")
                           or "graphql_error")
                if code in TRANSIENT_CODES:
                    reason = code
                    continue
                raise FirefliesError(what, code)
            if response.status_code >= 400:
                raise FirefliesError(what, f"http_{response.status_code}")
            return (body.get("data") if isinstance(body, dict) else None) or {}
        raise FirefliesUnavailable(what, reason)

    # --- the Transcripts port -------------------------------------------------------------------------
    def list_recent(self) -> list[TranscriptInfo]:
        data = self._query("list transcripts", LIST_QUERY, {"limit": self._limit})
        return [TranscriptInfo(str(t["id"]), str(t.get("title") or ""), parse_date(t))
                for t in data.get("transcripts") or [] if t and t.get("id")]

    def list_ready(self) -> list[str]:
        return [t.id for t in self.list_recent()]

    def get(self, transcript_id: str) -> Transcript | None:
        data = self._query("fetch transcript", GET_QUERY, {"id": transcript_id})
        item = data.get("transcript")
        if not item:
            return None
        sentences = tuple(
            Sentence(int(s.get("index", i)), str(s.get("speaker_name") or "Unknown"),
                     str(s.get("text") or ""), float(s.get("start_time") or 0),
                     float(s.get("end_time") or 0))
            for i, s in enumerate(item.get("sentences") or []) if s and str(s.get("text") or "").strip())
        summary = item.get("summary") or {}
        overview = str(summary.get("overview") or "").strip() if isinstance(summary, dict) else ""
        return Transcript(str(item["id"]), str(item.get("title") or ""), parse_date(item),
                          tuple(str(p) for p in item.get("participants") or [] if p), sentences, overview)
