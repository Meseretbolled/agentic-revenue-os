"""Turn web search results into cited facts about one lead, rejecting everything that is not that lead.

The flow is pure: the caller supplies `search` and `fetch` functions (the agent supplies ones that go through
its scoped, fenced tools; the `revos enrich` command supplies ones that fence the text itself), so the same
rules run in both places. The call caps come from the pack's web settings. Facts come from the pack's own
extractors, each with the sentence it was found in, its source URL and the time it was fetched."""

from collections.abc import Callable
from dataclasses import dataclass, field

from revos.segments.schema import Extractor, WebResearch
from revos.skills.entity_match import decide
from revos.skills.extraction import extract_fields


@dataclass(frozen=True)
class HitView:
    title: str
    url: str
    text: str
    fetched_at: str = ""
    origin: str = ""
    note: str = ""


@dataclass(frozen=True)
class PageView:
    url: str
    text: str | None          # None: the page was withheld (it tried to instruct the system)
    fetched_at: str = ""
    origin: str = ""


@dataclass(frozen=True)
class WebFact:
    field: str
    value: int | str | bool
    quote: str
    url: str
    fetched_at: str


@dataclass(frozen=True)
class Rejection:
    url: str
    reasons: tuple[str, ...]
    origin: str


@dataclass
class WebOutcome:
    facts: list[WebFact] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    accepted_urls: list[str] = field(default_factory=list)
    withheld: list[str] = field(default_factory=list)
    calls: int = 0
    fell_back: bool = False
    fallback_reason: str = ""


def run_web_enrichment(*, search: Callable[[str], list[HitView]], fetch: Callable[[str], PageView | None],
                       company: str, domain: str, location: str | None, web: WebResearch,
                       extractors: list[Extractor]) -> WebOutcome:
    out = WebOutcome()
    seen: set[str] = set()
    candidates: list[HitView] = []
    for template in web.queries[:web.max_searches]:
        hits = search(template.format(company=company, domain=domain))
        out.calls += 1
        for hit in hits:
            if hit.origin == "mock_fallback" and not out.fell_back:
                out.fell_back, out.fallback_reason = True, hit.note or "fell back to the offline mock"
            if not hit.url or hit.url in seen:
                continue
            seen.add(hit.url)
            verdict = decide(url=hit.url, text=hit.text, title=hit.title, company=company, domain=domain,
                             location=location)
            if verdict.accepted:
                candidates.append(hit)
            else:
                out.rejected.append(Rejection(hit.url, verdict.reasons, hit.origin or "tavily"))

    found: dict[str, WebFact] = {}

    def keep(text: str, url: str, fetched_at: str) -> None:
        for item in extract_fields(text, extractors):
            found.setdefault(item.field, WebFact(item.field, item.value, item.quote, url, fetched_at))

    for hit in candidates:
        out.accepted_urls.append(hit.url)
        keep(hit.text, hit.url, hit.fetched_at)
    for hit in candidates[:web.max_page_fetches]:
        page = fetch(hit.url)
        out.calls += 1
        if page is None:
            continue
        if page.text is None:
            out.withheld.append(hit.url)
            continue
        verdict = decide(url=page.url, text=page.text, company=company, domain=domain, location=location)
        if not verdict.accepted:
            out.rejected.append(Rejection(page.url, tuple(f"page {r}" for r in verdict.reasons),
                                          page.origin or "tavily"))
            continue
        keep(page.text, page.url, page.fetched_at or hit.fetched_at)
    out.facts = list(found.values())
    if not out.facts and not out.fell_back:
        out.fell_back = True
        out.fallback_reason = ("the search returned nothing for this company" if not candidates
                               else "no accepted page held a fact the pack looks for")
    return out
