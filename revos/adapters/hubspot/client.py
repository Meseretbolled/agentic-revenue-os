"""A small HubSpot REST client: timeouts, retries with backoff on 429 and 5xx, pagination.

The token is held privately and is never put in an error, a log line or a repr. Errors carry the
status, a category, and for a 4xx HubSpot's own message, its category and the NAMES of invalid
properties (truncated, token scrubbed). Never property values, headers or the token."""

import random
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from revos.domain.errors import PermanentError, RevosError

DEFAULT_BASE_URL = "https://api.hubapi.com"
EU_BASE_URL = "https://api-eu1.hubapi.com"
RETRY_STATUSES = {429, 500, 502, 503, 504}


def category_for(status: int) -> str:
    return {400: "bad_request", 401: "auth", 403: "forbidden", 404: "not_found", 409: "conflict",
            429: "rate_limited"}.get(status, "server_error" if status >= 500 else f"http_{status}")


MAX_DETAIL = 400


def error_detail(body: Any, token: str = "") -> str:
    """What HubSpot said was wrong, from an error body: its message, its category and the names of
    the invalid properties. Never a property value, and never the token."""
    if not isinstance(body, dict):
        return ""
    parts: list[str] = []
    if isinstance(body.get("message"), str):
        parts.append(body["message"])
    if isinstance(body.get("category"), str):
        parts.append(f"category={body['category']}")
    names = sorted({str(i["name"]) for i in body.get("errors", []) if isinstance(i, dict) and i.get("name")}
                   | {str(i["name"]) for i in body.get("context", {}).get("properties", [])
                      if isinstance(i, dict) and i.get("name")}
                   | {str(n) for n in (body.get("context") or {}).get("propertyName", []) if n}
                   ) if isinstance(body.get("context", {}), dict) else []
    if names:
        parts.append("invalid properties: " + ", ".join(names))
    scopes = sorted({str(sc) for e in body.get("errors", []) if isinstance(e, dict)
                     for sc in (e.get("context") or {}).get("requiredGranularScopes", [])
                     } if isinstance(body.get("errors"), list) else set())
    if scopes:                                  # a missing-scope answer names what the key needs: say so
        parts.append("any of these scopes is needed: " + ", ".join(scopes))
    text = " | ".join(parts)
    if token:
        text = text.replace(token, "[token]")
    return " ".join(text.split())[:MAX_DETAIL]


class HubSpotError(PermanentError):
    """A request HubSpot refused for good (a 4xx other than 429). Never retried."""

    def __init__(self, status: int, what: str, detail: str = "") -> None:
        self.status = status
        self.category = category_for(status)
        self.detail = detail
        super().__init__(f"HubSpot {what} failed: {status} {self.category}"
                         + (f" ({detail})" if detail else ""))


class HubSpotUnavailable(RevosError):
    """HubSpot could not be reached or kept failing after the retries. Safe to retry later."""

    def __init__(self, what: str, why: str) -> None:
        self.category = why
        super().__init__(f"HubSpot {what} unavailable: {why}")


class HubSpotClient:
    def __init__(self, token: str, base_url: str = DEFAULT_BASE_URL, *,
                 http: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep,
                 rng: random.Random | None = None, max_retries: int = 4, timeout: float = 15.0) -> None:
        self._token = token
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=timeout)
        self._sleep, self._rng = sleep, rng or random.Random()
        self.max_retries, self.timeout = max_retries, timeout
        self.requests_made = 0

    def __repr__(self) -> str:                     # never show the token
        return f"HubSpotClient(base_url={self.base_url!r})"

    def _detail(self, response: httpx.Response) -> str:
        try:
            return error_detail(response.json(), self._token)
        except ValueError:
            return ""

    def _delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after and retry_after.replace(".", "", 1).isdigit():
            return min(60.0, float(retry_after))
        return min(30.0, 0.5 * 2**attempt) * (0.5 + self._rng.random() / 2)

    def request(self, method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None,
                what: str | None = None, retry_ambiguous: bool = True) -> Any:
        """One API call. Retries 429 and 5xx (and timeouts) with backoff; raises on anything else.

        `retry_ambiguous=False` is for creates: a timeout, a dropped connection or a 5xx may mean
        HubSpot DID create the record, so only a 429 (never processed) is retried. The caller then
        looks the record up before trying again."""
        label = what or f"{method} {path.split('?')[0]}"
        reason, retry_after = "network", None
        for attempt in range(self.max_retries + 1):
            if attempt:
                self._sleep(self._delay(attempt - 1, retry_after))
            retry_after = None
            self.requests_made += 1
            try:
                response = self._http.request(
                    method, self.base_url + path, json=json, params=params, timeout=self.timeout,
                    headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"})
            except httpx.TimeoutException:
                reason = "timeout"
                if not retry_ambiguous:
                    break
                continue
            except httpx.TransportError:
                reason = "network"
                if not retry_ambiguous:
                    break
                continue
            if response.status_code in RETRY_STATUSES:
                if not retry_ambiguous and response.status_code != 429:
                    reason = category_for(response.status_code)
                    break
                reason = category_for(response.status_code)
                retry_after = response.headers.get("Retry-After")
                continue
            if response.status_code >= 400:
                raise HubSpotError(response.status_code, label, self._detail(response))
            if response.status_code == 204 or not response.content:
                return None
            return response.json()
        raise HubSpotUnavailable(label, reason)

    def search_all(self, object_type: str, filters: list[dict[str, Any]], properties: list[str],
                   *, page_size: int = 100, max_pages: int = 200) -> Iterator[dict[str, Any]]:
        """Every match, following the paging cursor to the end."""
        after: str | None = None
        for _ in range(max_pages):
            body: dict[str, Any] = {"filterGroups": [{"filters": filters}], "properties": properties,
                                    "limit": page_size}
            if after:
                body["after"] = after
            page = self.request("POST", f"/crm/v3/objects/{object_type}/search", json=body,
                                what=f"search {object_type}")
            yield from page.get("results", [])
            after = (page.get("paging") or {}).get("next", {}).get("after")
            if not after:
                return

    def list_all(self, object_type: str, properties: list[str], *, page_size: int = 100,
                 max_pages: int = 200) -> Iterator[dict[str, Any]]:
        after: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": page_size, "properties": ",".join(properties)}
            if after:
                params["after"] = after
            page = self.request("GET", f"/crm/v3/objects/{object_type}", params=params,
                                what=f"list {object_type}")
            yield from page.get("results", [])
            after = (page.get("paging") or {}).get("next", {}).get("after")
            if not after:
                return
