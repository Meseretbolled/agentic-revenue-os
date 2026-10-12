"""A web result is accepted only if it is on the lead's domain, names the company, and (when known) the place."""

import pytest

from revos.skills.entity_match import (
    decide,
    distinctive_tokens,
    host_matches,
    location_matches,
    name_matches,
)


@pytest.mark.parametrize("url,ok", [
    ("https://acme-co.example/about", True),
    ("https://www.acme-co.example/", True),
    ("https://shop.acme-co.example/x", True),
    ("https://acme-co.example.evil.io/about", False),       # the domain only appears as a prefix of another
    ("https://notacme-co.example/about", False),
    ("https://evil.io/acme-co.example", False),             # the domain only appears in the path
    ("not a url", False),
])
def test_only_the_leads_own_domain_matches(url, ok) -> None:
    assert host_matches(url, "acme-co.example") is ok


def test_the_name_must_name_the_company_not_just_its_kind() -> None:
    assert distinctive_tokens("Hawke Media") == ["hawke"]
    assert name_matches("Hawke Media is a performance agency", "Hawke Media")
    assert not name_matches("A leading media and marketing agency", "Hawke Media")     # kind words only
    assert distinctive_tokens("The Media Group") == ["the", "media", "group"]          # all generic: keep all


def test_a_known_location_must_appear_and_an_unknown_one_is_not_required() -> None:
    assert location_matches("Based in Boca Raton, Florida.", "Boca Raton, FL")
    assert not location_matches("Based in Austin, Texas.", "Boca Raton, FL")
    assert decide(url="https://acme-co.example/", text="Acme Co sells things", company="Acme Co",
                  domain="acme-co.example", location=None).accepted


def test_each_failure_is_reported_with_its_reason() -> None:
    got = decide(url="https://other.example/a", text="Something else entirely", company="Acme Co",
                 domain="acme-co.example", location="Boston")
    assert not got.accepted
    assert [r.split(":")[0] for r in got.reasons] == ["domain", "name", "location"]


def test_the_known_wrong_company_case_unrelated_real_companies_are_all_rejected() -> None:
    """A search for an invented agency returned real, unrelated agencies. None may be accepted for the lead."""
    results = [
        ("https://realagency-one.example/about", "RealAgency One is a marketing agency serving 50 clients."),
        ("https://www.realagency-two.example/", "A creative agency in Texas with 12 clients and 4 channels."),
        ("https://directory.example/tidewater", "Directory listing: Tidewater Creative Co, an agency."),
        ("https://tidewater-creative.example.evil.io/", "Tidewater Creative Co is an agency with 14 clients."),
    ]
    for url, text in results:
        verdict = decide(url=url, text=text, company="Tidewater Creative Co",
                         domain="tidewater-creative.example")
        assert not verdict.accepted, url
        assert any(r.startswith("domain:") for r in verdict.reasons)
