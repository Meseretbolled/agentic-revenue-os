import pytest

from revos.adapters.fake_llm import FakeLLM
from revos.composition import build_llm
from revos.domain.drafting import Evidence, EvidenceItem
from revos.domain.untrusted import UntrustedText
from revos.ports.llm import DraftRequest

REPLIES = [
    ("Please unsubscribe me from this list", "unsubscribe"),
    ("I am out of office until Monday", "out_of_office"),
    ("Talk to my colleague Priya, she is the better person", "referral"),
    ("This is too expensive for our budget", "price_objection"),
    ("No thanks, not interested", "not_interested"),
    ("Sounds good, let's talk next week", "interested"),
    ("Do you work with franchises in Europe?", "question"),
]


@pytest.mark.parametrize("text,label", REPLIES)
def test_classification_is_deterministic_and_rule_based(text, label) -> None:
    u = UntrustedText.wrap("reply", "m", text)
    first, second = FakeLLM().classify_reply(u), FakeLLM().classify_reply(u)
    assert first == second and first.label == label


def test_drafting_is_deterministic() -> None:
    ev = Evidence.of([EvidenceItem("F1", "Acme Optical operates 120 locations")])
    request = DraftRequest("s", "b", {}, ev)
    assert FakeLLM().draft_email(request) == FakeLLM().draft_email(request)
    assert FakeLLM().draft_email(DraftRequest("s", "b", {}, Evidence())).claims == ()


def test_the_whole_suite_needs_no_key_and_no_network(monkeypatch) -> None:
    for var in ("ANTHROPIC_API_KEY", "LLM_MODE"):
        monkeypatch.delenv(var, raising=False)
    llm = build_llm()
    assert isinstance(llm, FakeLLM)
    assert llm.classify_reply(UntrustedText.wrap("reply", "m", "sounds good")).label == "interested"


def test_build_llm_rejects_unknown_modes_and_live_without_a_ledger(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODE", "turbo")
    with pytest.raises(ValueError, match="fake"):
        build_llm()
    monkeypatch.setenv("LLM_MODE", "live")
    with pytest.raises(ValueError, match="ledger"):
        build_llm(None)
