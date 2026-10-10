import re

from revos.adapters.mock.fixtures import load
from revos.ports.research import FetchedPage, SearchHit


class MockResearch:
    def __init__(self) -> None:
        data = load("research_pages")
        self._pages = {p["url"]: " ".join(p["html"].split()) for p in data["pages"]}
        self._hits = data["search_hits"]
        self.searches: list[str] = []
        self.fetches: list[str] = []

    def search(self, query: str, max_results: int = 5) -> list[SearchHit]:
        self.searches.append(query)
        words = set(re.findall(r"[a-z0-9]+", query.casefold()))
        hits: list[SearchHit] = []
        for group in self._hits:
            if words & set(group["match"]):
                hits += [SearchHit(h["title"], h["url"], h["snippet"]) for h in group["hits"]]
        return hits[:max_results]

    def fetch(self, url: str) -> FetchedPage | None:
        self.fetches.append(url)
        html = self._pages.get(url)
        return FetchedPage(url, html, True) if html else None
