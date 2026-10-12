"""Tavily as a Research adapter. Same port as the offline mock.

* Every answer is cached with its fetch time; `replay` uses the cache only and never touches the network.
* A total credit cap stops the calls before they are made.
* A failing, capped or empty search falls back to the offline mock and says so (origin "mock_fallback"
  and a note), so the caller can audit it. A fetch is only ever sent to Tavily for a URL that Tavily's own
  search returned in this run; any other URL goes to the mock, so nothing can be fetched by guessing.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from revos.adapters.tavily.cache import ResearchCache
from revos.adapters.tavily.client import ResearchUnavailable, TavilyClient
from revos.ports.research import FetchedPage, Research, SearchHit

PAGE_CHARS = 20_000


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class TavilyResearch:
    def __init__(self, client: TavilyClient | None, cache: ResearchCache, fallback: Research, *,
                 max_credits: int = 20, replay: bool = False,
                 now: Callable[[], str] = _now) -> None:
        if client is None and not replay:
            raise ValueError("live research needs a Tavily client; use replay to run from the cache only")
        self._client, self._cache, self._fallback = client, cache, fallback
        self._max, self._replay, self._now = max_credits, replay, now
        self._seen: set[str] = set()
        self.credits_used = 0
        self.live_calls = 0
        self.cache_hits = 0

    # --- the port ---------------------------------------------------------------------------------
    def search(self, query: str, max_results: int = 5) -> list[SearchHit]:
        params = {"query": query, "max_results": max_results}
        try:
            entry = self._answer("search", params, lambda: self._client.search(query, max_results), 1)
        except ResearchUnavailable as exc:
            return self._fallback_search(query, max_results, str(exc))
        hits = [SearchHit(str(r.get("title", "")), str(r.get("url", "")), str(r.get("content", "")),
                          entry["fetched_at"], "tavily")
                for r in entry["response"].get("results", []) if r.get("url")]
        if not hits:
            return self._fallback_search(query, max_results, "Tavily returned no results")
        self._seen.update(h.url for h in hits)
        return hits[:max_results]

    def fetch(self, url: str) -> FetchedPage | None:
        if url not in self._seen:                       # never fetch a URL Tavily did not just give us
            return self._fallback.fetch(url)
        params = {"urls": [url]}
        try:
            entry = self._answer("extract", params, lambda: self._client.extract([url]), 1)
        except ResearchUnavailable:
            return self._fallback.fetch(url)
        for item in entry["response"].get("results", []):
            if item.get("url") == url and item.get("raw_content"):
                return FetchedPage(url, str(item["raw_content"])[:PAGE_CHARS], False,
                                   entry["fetched_at"], "tavily")
        return self._fallback.fetch(url)

    # --- internals --------------------------------------------------------------------------------
    def _answer(self, op: str, params: dict[str, Any], call: Callable[[], dict[str, Any]],
                estimate: int) -> dict[str, Any]:
        cached = self._cache.get(op, params)
        if cached is not None:
            self.cache_hits += 1
            return cached
        if self._replay:
            raise ResearchUnavailable("replay mode: this request is not in the cache")
        if self.credits_used + estimate > self._max:
            raise ResearchUnavailable(f"credit cap of {self._max} reached; call not made")
        response = call()
        usage = response.get("usage") or {}
        credits = int(usage.get("credits", estimate)) if isinstance(usage, dict) else estimate
        self.credits_used += max(credits, 0)
        self.live_calls += 1
        fetched_at = self._now()
        self._cache.put(op, params, response, fetched_at, credits)
        return {"response": response, "fetched_at": fetched_at, "credits": credits}

    def _fallback_search(self, query: str, max_results: int, why: str) -> list[SearchHit]:
        """The offline mock's answer, marked as a fallback. If the mock has nothing either, one marker hit
        with no URL is returned, so the caller still learns that the real search did not deliver."""
        note = f"fell back to the offline mock: {why}"
        hits = [SearchHit(h.title, h.url, h.snippet, "", "mock_fallback", note)
                for h in self._fallback.search(query, max_results)]
        return hits or [SearchHit("", "", "", "", "mock_fallback", note)]
