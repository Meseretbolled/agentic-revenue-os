"""The two REAL Tavily recordings (2026-10-12): replayed offline, they give the same accepted and rejected results
every time, and no wrong-company result is ever accepted."""

import pytest

from revos.adapters.mock.research import MockResearch
from revos.adapters.tavily.cache import ResearchCache
from revos.adapters.tavily.research import TavilyResearch
from revos.composition import TAVILY_BUNDLED_CACHE
from revos.segments import load_pack
from revos.skills.entity_match import host_matches
from revos.skills.web_research import HitView, PageView, run_web_enrichment
from tests.conftest import PACKS_DIR

CASES = [("Orangetheory Fitness", "orangetheory.com", "franchise_brands"),
         ("Hawke Media", "hawkemedia.com", "agencies")]


def replay(company: str, domain: str, pack_id: str):
    pack = load_pack(PACKS_DIR / pack_id)
    research = TavilyResearch(None, ResearchCache(TAVILY_BUNDLED_CACHE), MockResearch(), replay=True)

    def search(q):
        return [HitView(h.title, h.url, h.snippet, h.fetched_at, h.origin, h.note) for h in research.search(q)]

    def fetch(url):
        page = research.fetch(url)
        return PageView(url, page.content, page.fetched_at, page.origin) if page else None

    out = run_web_enrichment(search=search, fetch=fetch, company=company, domain=domain, location=None,
                             web=pack.enrichment.web, extractors=pack.enrichment.extractors)
    return out, research


def test_the_recordings_are_marked_as_recorded_from_live_with_dates() -> None:
    readme = (TAVILY_BUNDLED_CACHE / "README.md").read_text()
    assert "LIVE" in readme and "2026-10-12" in readme and "NOT synthetic" in readme
    assert "live_2026-10-12" in TAVILY_BUNDLED_CACHE.name
    assert len(list(TAVILY_BUNDLED_CACHE.glob("*.json"))) == 4


@pytest.mark.parametrize("company,domain,pack_id", CASES)
def test_no_wrong_company_result_is_accepted_and_replay_costs_nothing(company, domain, pack_id) -> None:
    out, research = replay(company, domain, pack_id)
    assert out.accepted_urls and all(host_matches(u, domain) for u in out.accepted_urls)
    assert all(host_matches(f.url, domain) for f in out.facts)                # every fact is from the lead's site
    assert len(out.rejected) == 4 and not any(host_matches(r.url, domain) for r in out.rejected)
    assert research.live_calls == 0 and research.credits_used == 0 and research.cache_hits >= 2


def test_hawke_media_gives_one_cited_fact_with_its_source_and_fetch_time() -> None:
    out, _ = replay("Hawke Media", "hawkemedia.com", "agencies")
    [fact] = out.facts
    assert fact.field == "is_agency" and "Top Performance Marketing Agency" in fact.quote
    assert fact.url == "https://hawkemedia.com" and fact.fetched_at == "2026-10-12T13:48:40+00:00"


def test_orangetheory_gives_no_fact_because_the_sample_extractors_do_not_read_studios() -> None:
    out, _ = replay("Orangetheory Fitness", "orangetheory.com", "franchise_brands")
    assert out.facts == [] and out.fell_back and "no accepted page held a fact" in out.fallback_reason
    assert out.accepted_urls == ["https://www.orangetheory.com/en-us"]
