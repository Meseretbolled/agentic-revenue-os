from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str                 # untrusted
    fetched_at: str = ""         # when the provider answered (ISO time); empty for the offline mock
    origin: str = ""             # "tavily", or "mock_fallback" when the real provider failed or found nothing
    note: str = ""               # why it fell back, when it did


@dataclass(frozen=True)
class FetchedPage:
    url: str
    content: str                 # untrusted
    is_html: bool = True
    fetched_at: str = ""
    origin: str = ""


class Research(Protocol):
    def search(self, query: str, max_results: int = 5) -> list[SearchHit]: ...

    def fetch(self, url: str) -> FetchedPage | None: ...
