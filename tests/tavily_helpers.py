"""A fake Tavily client and small builders, so no test ever calls the network."""

from typing import Any

from revos.adapters.mock.research import MockResearch
from revos.adapters.tavily.cache import ResearchCache
from revos.adapters.tavily.client import ResearchUnavailable
from revos.adapters.tavily.research import TavilyResearch

FIXED_TIME = "2026-10-12T09:00:00+00:00"


class FakeTavily:
    """Stands in for TavilyClient. `fail` makes every call raise; counts calls like the real one is billed."""

    def __init__(self, hits: list[dict[str, Any]] | None = None, pages: dict[str, str] | None = None,
                 fail: str | None = None) -> None:
        self.hits, self.pages, self.fail = hits or [], pages or {}, fail
        self.searches: list[str] = []
        self.extracts: list[list[str]] = []

    def search(self, query: str, max_results: int) -> dict[str, Any]:
        self.searches.append(query)
        if self.fail:
            raise ResearchUnavailable(self.fail)
        return {"query": query, "results": self.hits[:max_results], "usage": {"credits": 1}}

    def extract(self, urls: list[str]) -> dict[str, Any]:
        self.extracts.append(urls)
        if self.fail:
            raise ResearchUnavailable(self.fail)
        return {"results": [{"url": u, "raw_content": self.pages[u]} for u in urls if u in self.pages],
                "usage": {"credits": 1}}


def hit(url: str, content: str, title: str = "") -> dict[str, Any]:
    return {"url": url, "title": title or url, "content": content, "score": 0.9}


def tavily_research(tmp_path, client: FakeTavily | None, *, replay: bool = False, max_credits: int = 20,
                    fallback=None) -> TavilyResearch:
    return TavilyResearch(client, ResearchCache(tmp_path / "cache"), fallback or MockResearch(),
                          max_credits=max_credits, replay=replay, now=lambda: FIXED_TIME)


# an invented company, so no real company's text is needed
COMPANY, DOMAIN = "Larkspur Fitness Franchising", "larkspur-fitness.example"
ON_SITE = (f"https://{DOMAIN}/about",
           "Larkspur Fitness Franchising is a franchise brand with 240 locations across Texas and Ohio.")
