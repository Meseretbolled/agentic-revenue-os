"""The Tavily adapter: cache and replay, credit cap, fallback, and that the key never leaks. No network."""

import httpx
import pytest

from revos.adapters.tavily.client import ResearchUnavailable, TavilyClient
from tests.tavily_helpers import DOMAIN, FIXED_TIME, ON_SITE, FakeTavily, hit, tavily_research

HITS = [hit(*ON_SITE)]


def test_a_search_is_cached_with_its_fetch_time_and_a_repeat_costs_nothing(tmp_path) -> None:
    client = FakeTavily(HITS)
    research = tavily_research(tmp_path, client)
    first = research.search("larkspur")
    second = research.search("larkspur")
    assert first == second and first[0].fetched_at == FIXED_TIME and first[0].origin == "tavily"
    assert client.searches == ["larkspur"] and research.credits_used == 1 and research.cache_hits == 1


def test_replay_answers_from_the_cache_alone_and_a_miss_falls_back(tmp_path) -> None:
    tavily_research(tmp_path, FakeTavily(HITS)).search("larkspur")             # records it
    replay = tavily_research(tmp_path, None, replay=True)
    assert replay.search("larkspur")[0].origin == "tavily"
    miss = replay.search("never recorded")
    assert miss[0].origin == "mock_fallback" and "replay mode" in miss[0].note


def test_a_failing_search_falls_back_and_says_why(tmp_path) -> None:
    research = tavily_research(tmp_path, FakeTavily(fail="Tavily rejected the key"))
    got = research.search("anything")
    assert [h.origin for h in got] == ["mock_fallback"] and "rejected the key" in got[0].note


def test_an_empty_answer_falls_back(tmp_path) -> None:
    got = tavily_research(tmp_path, FakeTavily([])).search("nothing matches this")
    assert got[0].origin == "mock_fallback" and "no results" in got[0].note


def test_the_credit_cap_stops_calls_before_they_are_made(tmp_path) -> None:
    client = FakeTavily(HITS)
    research = tavily_research(tmp_path, client, max_credits=1)
    research.search("one")
    got = research.search("two")                       # would be the second credit
    assert client.searches == ["one"] and got[0].origin == "mock_fallback" and "credit cap" in got[0].note


def test_only_urls_that_tavily_returned_are_ever_fetched(tmp_path) -> None:
    client = FakeTavily(HITS, pages={ON_SITE[0]: ON_SITE[1], f"https://{DOMAIN}/secret": "x"})
    research = tavily_research(tmp_path, client)
    assert research.fetch(f"https://{DOMAIN}/secret") is None            # never offered by a search: mock only
    research.search("larkspur")
    page = research.fetch(ON_SITE[0])
    assert page is not None and page.fetched_at == FIXED_TIME and page.origin == "tavily"
    assert client.extracts == [[ON_SITE[0]]]


def test_a_blocked_or_rejected_key_raises_without_the_key_in_the_message() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(403, text="<html>403 Forbidden</html>")

    client = TavilyClient("tvly-SECRET-KEY", httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ResearchUnavailable) as err:
        client.search("x", 3)
    assert "SECRET" not in str(err.value) and "rejected the key" in str(err.value)
    assert seen[0].headers["authorization"] == "Bearer tvly-SECRET-KEY" and "SECRET" not in str(seen[0].url)
    assert "SECRET" not in seen[0].content.decode()                       # never in the request body


def test_network_errors_do_not_leak_the_key_either() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom tvly-SECRET-KEY", request=request)

    client = TavilyClient("tvly-SECRET-KEY", httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ResearchUnavailable) as err:
        client.search("x", 3)
    assert "SECRET" not in str(err.value)


def test_live_research_needs_a_client_and_replay_does_not(tmp_path) -> None:
    with pytest.raises(ValueError):
        tavily_research(tmp_path, None)
    assert tavily_research(tmp_path, None, replay=True) is not None
