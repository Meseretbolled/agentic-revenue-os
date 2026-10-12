"""The Tavily HTTP API, and nothing else. The key is sent as a bearer token and is never logged,
printed or put in an error message."""

from typing import Any

import httpx

from revos.domain.errors import RevosError

BASE_URL = "https://api.tavily.com"
TIMEOUT = 25.0


class ResearchUnavailable(RevosError):
    """Tavily could not answer (network, auth, plan limit). The message never contains the key."""


class TavilyClient:
    def __init__(self, api_key: str, http: httpx.Client | None = None) -> None:
        self._key = api_key.strip().strip("\"'")
        self._http = http or httpx.Client()

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            got = self._http.post(f"{BASE_URL}{path}", json=body, timeout=TIMEOUT,
                                  headers={"Authorization": f"Bearer {self._key}"})
        except httpx.HTTPError as exc:
            raise ResearchUnavailable(f"Tavily request failed: {type(exc).__name__}") from None
        if got.status_code in (401, 403):
            raise ResearchUnavailable("Tavily rejected the key")
        if got.status_code in (432, 433, 429):
            raise ResearchUnavailable("Tavily plan or rate limit reached")
        if got.status_code != 200:
            raise ResearchUnavailable(f"Tavily answered HTTP {got.status_code}")
        try:
            data = got.json()
        except ValueError:
            raise ResearchUnavailable("Tavily answered with something that is not JSON") from None
        if not isinstance(data, dict):
            raise ResearchUnavailable("Tavily answered with an unexpected shape")
        return data

    def search(self, query: str, max_results: int) -> dict[str, Any]:
        return self._post("/search", {"query": query, "max_results": max_results,
                                      "search_depth": "basic", "include_usage": True})

    def extract(self, urls: list[str]) -> dict[str, Any]:
        return self._post("/extract", {"urls": urls, "include_usage": True})
