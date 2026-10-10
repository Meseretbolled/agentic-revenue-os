from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str                 # untrusted


@dataclass(frozen=True)
class FetchedPage:
    url: str
    content: str                 # untrusted
    is_html: bool = True


class Research(Protocol):
    def search(self, query: str, max_results: int = 5) -> list[SearchHit]: ...

    def fetch(self, url: str) -> FetchedPage | None: ...
