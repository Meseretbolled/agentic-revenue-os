"""A self-promotional superlative from a web fact must be attributed to the company, or it fails the check."""

import pytest

from revos.agents.common import evidence_from_fields
from revos.domain.drafting import Claim, Draft, Evidence, EvidenceItem
from revos.skills.claims import deterministic_check

QUOTE = '#### "Hawke Media is the Top Performance Marketing Agency in the U.S."'
WEB = {"fact:W1": QUOTE, "fact_source:W1": "https://hawkemedia.com", "fact_fetched:W1": "2026-10-12T13:48:40+00:00"}


def check(claim_text: str, fields=None, approved=("Cool AI",)):
    evidence = evidence_from_fields(fields or WEB)
    draft = Draft("Hi", f"Hi Riley, {claim_text} Cool AI helps.", (Claim(claim_text, ("W1",)),))
    return deterministic_check(draft, evidence, list(approved), ["Hawke Media", "Riley"])


def codes(issues) -> list[str]:
    return [i.code for i in issues]


def test_a_web_fact_is_marked_as_web_and_a_demo_or_plain_fact_is_not() -> None:
    items = evidence_from_fields({**WEB, "fact:F1": "x", "fact_source:F1": "https://a.example",
                                  "fact:DEMO1": "y", "fact_source:DEMO1": "fictional demo profile"}).items
    assert items["W1"].web and not items["F1"].web and not items["DEMO1"].web


@pytest.mark.parametrize("text", [
    "Hawke Media is the Top Performance Marketing Agency in the U.S.",
    "Hawke Media is the leading performance agency.",
    "Hawke Media is the best agency around.",
    "Hawke Media is the number one agency in the U.S.",
    "Hawke Media is the largest independent agency.",
])
def test_an_unattributed_superlative_from_a_web_fact_is_rejected(text) -> None:
    assert "unattributed_superlative" in codes(check(text))


@pytest.mark.parametrize("text", [
    "Hawke Media describes itself as the Top Performance Marketing Agency in the U.S.",
    "Hawke Media calls itself the leading performance agency.",
    "Hawke Media says it is the best agency around.",
    "According to its website, Hawke Media is the top performance agency.",
    "Hawke Media claims to be the number one agency in the U.S.",
])
def test_an_attributed_superlative_passes(text) -> None:
    assert "unattributed_superlative" not in codes(check(text))


def test_a_plain_statement_from_a_web_fact_needs_no_attribution() -> None:
    fields = {"fact:W1": "Hawke Media is a marketing agency.", "fact_source:W1": "https://hawkemedia.com",
              "fact_fetched:W1": "2026-10-12T13:48:40+00:00"}
    assert check("Hawke Media is a marketing agency.", fields) == []


def test_the_rule_applies_only_to_web_facts_so_existing_evidence_is_unchanged() -> None:
    plain = Evidence.of([EvidenceItem("F1", "Acme is the leading optical brand.", "https://acme.example")])
    draft = Draft("Hi", "Acme is the leading optical brand.",
                  (Claim("Acme is the leading optical brand.", ("F1",)),))
    assert "unattributed_superlative" not in codes(deterministic_check(draft, plain, [], ["Acme"]))


def test_the_failure_message_tells_the_drafter_how_to_fix_it() -> None:
    [issue] = [i for i in check("Hawke Media is the best agency around.") if i.code == "unattributed_superlative"]
    assert "describes itself" in issue.message or "say the company describes itself" in issue.message
