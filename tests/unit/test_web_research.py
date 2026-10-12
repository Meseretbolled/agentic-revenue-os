"""The web enrichment flow: caps, rejections, fallbacks, and what a stored fact carries."""

from revos.segments import load_pack
from revos.skills.web_research import HitView, PageView, run_web_enrichment
from tests.conftest import PACKS_DIR
from tests.tavily_helpers import COMPANY, DOMAIN, FIXED_TIME, ON_SITE

PACK = load_pack(PACKS_DIR / "franchise_brands")


def run(search, fetch, location=None, **web):
    cfg = PACK.enrichment.web.model_copy(update=web) if web else PACK.enrichment.web
    return run_web_enrichment(search=search, fetch=fetch, company=COMPANY, domain=DOMAIN, location=location,
                              web=cfg, extractors=PACK.enrichment.extractors)


def good_hit(**kw):
    url, text = ON_SITE
    return HitView("About", url, text, FIXED_TIME, "tavily", **kw)


def test_an_accepted_page_gives_cited_facts_with_source_and_time() -> None:
    out = run(lambda q: [good_hit()], lambda u: PageView(u, ON_SITE[1], FIXED_TIME, "tavily"))
    fields = {f.field: f for f in out.facts}
    assert fields["location_count"].value == 240 and fields["is_franchise"].value is True
    assert all(f.url == ON_SITE[0] and f.fetched_at == FIXED_TIME and f.quote for f in out.facts)
    assert not out.fell_back and out.rejected == []


def test_wrong_company_results_are_rejected_and_yield_no_fact() -> None:
    other = HitView("Other", "https://other-fitness.example/about",
                    "Other Fitness has 900 locations and is a franchise.", FIXED_TIME, "tavily")
    out = run(lambda q: [other], lambda u: None)
    assert out.facts == [] and out.fell_back
    assert [r.url for r in out.rejected] == [other.url] and out.rejected[0].reasons[0].startswith("domain:")


def test_a_page_on_the_right_domain_that_does_not_name_the_company_is_rejected() -> None:
    page = PageView(ON_SITE[0], "We have 500 locations and we are a franchise.", FIXED_TIME, "tavily")
    out = run(lambda q: [good_hit()], lambda u: page)
    # the search snippet was fine, so its facts stand; the page that does not name the company adds none
    assert [r.url for r in out.rejected] == [ON_SITE[0]]
    assert out.rejected[0].reasons[0].startswith("page name:")
    assert {f.field: f.value for f in out.facts}["location_count"] == 240


def test_a_known_location_that_the_text_does_not_name_rejects_the_result() -> None:
    out = run(lambda q: [good_hit()], lambda u: None, location="Boca Raton, FL")
    assert out.facts == [] and out.rejected[0].reasons[0].startswith("location:")


def test_the_call_caps_are_honoured() -> None:
    hits = [HitView("p", f"https://{DOMAIN}/p{i}", ON_SITE[1], FIXED_TIME, "tavily") for i in range(5)]
    fetched: list[str] = []

    def fetch(url):
        fetched.append(url)
        return PageView(url, ON_SITE[1], FIXED_TIME, "tavily")

    out = run(lambda q: hits, fetch, max_searches=1, max_page_fetches=2)
    assert len(fetched) == 2 and out.calls == 3             # one search plus two page fetches, never more


def test_a_withheld_page_is_listed_and_never_read() -> None:
    out = run(lambda q: [good_hit()], lambda u: PageView(u, None))
    assert out.withheld == [ON_SITE[0]]


def test_an_empty_search_and_a_mock_fallback_are_reported_as_a_fallback() -> None:
    assert run(lambda q: [], lambda u: None).fell_back
    marker = HitView("", "", "", "", "mock_fallback", "fell back to the offline mock: Tavily rejected the key")
    out = run(lambda q: [marker], lambda u: None)
    assert out.fell_back and "Tavily rejected the key" in out.fallback_reason and out.facts == []
