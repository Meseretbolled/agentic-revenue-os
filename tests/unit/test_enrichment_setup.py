"""ENRICHMENT_MODE, the real-company seed file, the Tavily mode line, and `revos enrich` replay. No network."""

import pytest
import yaml
from typer.testing import CliRunner

from revos import composition
from revos.adapters.mock.research import MockResearch
from revos.adapters.tavily.cache import ResearchCache
from revos.adapters.tavily.research import TavilyResearch
from revos.cli.main import app
from revos.modes import REAL, integration_modes
from revos.policy.recipients import base_of
from revos.seed import PROFILES_FILE, REAL_PROFILES_FILE, demo_address, real_profile
from revos.segments import load_all
from tests.conftest import PACKS_DIR
from tests.tavily_helpers import COMPANY, DOMAIN, FIXED_TIME, ON_SITE


def test_the_default_is_the_mock_and_the_mock_object_is_returned_unchanged() -> None:
    mock = MockResearch()
    assert composition.enrichment_mode({}) == "mock" and composition.enrichment_mode({"ENRICHMENT_MODE": ""}) == "mock"
    research, mode = composition.build_research(mock, {})
    assert research is mock and mode == "mock"


def test_an_unknown_mode_is_an_error_not_a_silent_mock() -> None:
    with pytest.raises(ValueError, match="ENRICHMENT_MODE"):
        composition.build_research(MockResearch(), {"ENRICHMENT_MODE": "web"})


def test_tavily_mode_needs_a_key_or_an_explicit_replay() -> None:
    with pytest.raises(ValueError, match="TAVILY_API_KEY"):
        composition.build_research(MockResearch(), {"ENRICHMENT_MODE": "tavily"})
    research, mode = composition.build_research(MockResearch(), {"ENRICHMENT_MODE": "tavily",
                                                                 "ENRICHMENT_CACHE": "replay"})
    assert isinstance(research, TavilyResearch) and mode == "tavily"
    live, _ = composition.build_research(MockResearch(), {"ENRICHMENT_MODE": "tavily", "TAVILY_API_KEY": "k",
                                                          "TAVILY_MAX_CREDITS": "3"})
    assert isinstance(live, TavilyResearch)


def test_the_tavily_mode_line_says_real_only_when_switched_on() -> None:
    def tavily(env):
        return next(m for m in integration_modes(env) if m.service == "Tavily")

    assert tavily({"TAVILY_API_KEY": "k"}).mode == "MOCK" and not tavily({"TAVILY_API_KEY": "k"}).built
    assert tavily({"TAVILY_API_KEY": "k", "ENRICHMENT_MODE": "tavily"}).mode == REAL
    no_key = tavily({"ENRICHMENT_MODE": "tavily"})
    assert no_key.mode == "MOCK" and "refuses to start" in no_key.why


# --- the real-company seed file ---------------------------------------------------------------------------------

def test_the_real_seed_file_has_both_companies_with_the_owners_own_inbox(monkeypatch) -> None:
    monkeypatch.setenv("PROSPECT_TEST_EMAIL", "owner@gmail.com")
    packs = load_all(PACKS_DIR.parent / "packs")
    seen = {}
    for name in ("orangetheory", "hawkemedia"):
        segment, profile = real_profile(name)
        assert segment in packs                                  # an existing pack, no new one
        address = demo_address(profile["email_tag"])
        assert address.startswith("owner+real-") and base_of(address) == "owner@gmail.com"
        assert not address.endswith(profile["domain"])           # never anyone at the company
        assert "research_notes" not in profile                   # no company fact is written in the file
        seen[name] = (segment, profile["domain"])
    assert seen == {"orangetheory": ("franchise_brands", "orangetheory.com"),
                    "hawkemedia": ("agencies", "hawkemedia.com")}


def test_the_real_seed_file_states_its_honesty_rules_and_the_fixture_leads_are_not_in_it() -> None:
    text = REAL_PROFILES_FILE.read_text()
    assert "SYNTHETIC CRM SIGNALS" in text and "never anyone at the company" in text
    real_names = {n for v in yaml.safe_load(text).values() for n in v}
    fixture_names = {n for v in yaml.safe_load(PROFILES_FILE.read_text()).values() for n in v}
    assert not real_names & fixture_names                        # a separate file: the fixtures are untouched


def test_an_unknown_real_profile_names_the_ones_that_exist() -> None:
    with pytest.raises(ValueError, match="orangetheory"):
        real_profile("nope")


# --- revos enrich, replayed from a recorded cache ---------------------------------------------------------------

def record(cache_dir) -> None:
    cache = ResearchCache(cache_dir)
    query = f"{COMPANY} {DOMAIN}"
    cache.put("search", {"query": query, "max_results": 5}, {"results": [
        {"url": ON_SITE[0], "title": "About", "content": ON_SITE[1]},
        {"url": "https://other-gym.example/", "title": "Other", "content": "Other Gym, 50 locations."}]},
        FIXED_TIME, 1)
    cache.put("extract", {"urls": [ON_SITE[0]]}, {"results": [{"url": ON_SITE[0], "raw_content": ON_SITE[1]}]},
              FIXED_TIME, 1)


def test_enrich_replay_prints_accepted_facts_with_sources_and_rejected_ones_with_reasons(
        tmp_path, monkeypatch) -> None:
    record(tmp_path)
    monkeypatch.setattr(composition, "TAVILY_CACHE_DIR", tmp_path)
    monkeypatch.setattr(composition, "TAVILY_BUNDLED_CACHE", tmp_path / "none")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    result = CliRunner().invoke(app, ["enrich", COMPANY, "--domain", DOMAIN, "--segment", "franchise_brands",
                                      "--replay"])
    out = result.output
    assert result.exit_code == 0, out
    assert "location_count = 240" in out and f"source {ON_SITE[0]}" in out and f"fetched {FIXED_TIME}" in out
    assert "https://other-gym.example/" in out and "domain: other-gym.example is not" in out
    assert "live calls 0" in out and "credits used 0" in out          # replay never touches the network


def test_enrich_without_a_key_and_without_replay_refuses(monkeypatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    result = CliRunner().invoke(app, ["enrich", COMPANY, "--domain", DOMAIN])
    assert result.exit_code == 1 and "--replay" in result.output
