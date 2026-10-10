import pytest

from revos.adapters.fake_llm import FakeLLM
from revos.domain.drafting import Claim, Draft, Evidence, EvidenceItem
from revos.domain.errors import LLMError, SpendCapExceeded
from revos.ports.llm import ClaimVerdict
from revos.skills.claims import check_draft, deterministic_check, extract_tokens

EV = Evidence.of([
    EvidenceItem("F1", "Acme Optical operates 120 locations across Texas", "https://a.example"),
    EvidenceItem("F2", "Acme Optical opened a Boise office in March 2026", "https://a.example/n"),
    EvidenceItem("S1", "Acme Optical is hiring a Fleet Lead", "https://a.example/j", stale=True),
])
APPROVED = ["Hi {{ first_name }}, would a short call be useful? Cool AI covers Create, Activate and "
            "Optimize. Global Beauty Brand saw ROAS $15.52 vs $3.80 in 25 languages."]
NAMES = ["Sam Lee", "Jo Rivers", "Acme Optical", "VP Marketing"]


def draft(body: str, *claims: Claim, subject: str = "Hello") -> Draft:
    return Draft(subject, body, tuple(claims))


def codes(issues) -> list[str]:
    return [i.code for i in issues]


def kinds(text: str) -> list[tuple[str, str]]:
    return [(t.kind, t.key) for t in extract_tokens(text)]


# --- extraction is done by code ---------------------------------------------------------------

def test_numbers_dates_and_names_are_extracted() -> None:
    assert ("number", "$15.52") in kinds("ROAS of $15.52 vs $3.80")
    assert ("number", "17.5%") in kinds("17.5% more branded search")
    assert ("number", "1000") in kinds("1,000 stores")
    assert ("number", "120") in kinds("120.0 stores") or ("number", "120") in kinds("120 stores")
    assert ("number", "25") in kinds("twenty-five markets")
    assert ("number", "3") in kinds("three offices")
    assert ("date", "march") in kinds("opened in March")
    assert ("date", "september") in kinds("on Sept. 4")
    assert ("number", "2026") in kinds("in 2026")
    assert ("name", "boise") in kinds("a new Boise office")
    assert ("name", "acme") in kinds("Acme's growth")
    assert not [k for k in kinds("we may help") if k[0] == "date"]
    assert ("date", "may") in kinds("in May")


def test_common_greeting_words_are_not_names() -> None:
    assert [k for k in kinds("Hi there. Thanks, and happy Monday.") if k[0] == "name"] == []


# --- a good draft passes ----------------------------------------------------------------------

GOOD_LINE = "I noticed that Acme Optical operates 120 locations across Texas"
GOOD = draft(f"Hi Sam,\n\n{GOOD_LINE}\n\nWould a short call be useful?\n\nJo Rivers",
             Claim(GOOD_LINE, ("F1",)))


def test_a_supported_claim_passes_the_deterministic_check() -> None:
    assert deterministic_check(GOOD, EV, APPROVED, NAMES) == []


def test_good_draft_passes_with_a_verifier() -> None:
    result = check_draft(GOOD, EV, APPROVED, NAMES, FakeLLM())
    assert result.passed and result.verifier == "ok"


def test_approved_text_may_state_its_numbers_outside_claims() -> None:
    body = "Hi Sam, Global Beauty Brand saw ROAS $15.52 vs $3.80. Would a short call be useful?"
    assert deterministic_check(draft(body), EV, APPROVED, NAMES) == []


# --- undeclared claims FAIL -----------------------------------------------------------------------

@pytest.mark.parametrize("line,fact", [
    ("Congrats on your 130 locations.", "130"),                      # number not anywhere
    ("Saw you opened an office in Denver.", "Denver"),               # name
    ("You expanded in November.", "November"),                       # date
    ("You raised $5M.", "$5m"),                                      # money
    ("I read twelve stores closed.", "12"),                          # spelled-out number
])
def test_a_fact_outside_any_declared_claim_fails_the_draft(line, fact) -> None:
    issues = deterministic_check(draft(f"Hi Sam,\n{line}\nWould a short call be useful?"), EV,
                                 APPROVED, NAMES)
    assert "undeclared_fact" in codes(issues), issues


def test_undeclared_fact_next_to_a_good_claim_still_fails() -> None:
    body = f"Hi Sam,\n{GOOD_LINE}. You also employ 900 people.\nWould a short call be useful?"
    issues = deterministic_check(draft(body, Claim(GOOD_LINE, ("F1",))), EV, APPROVED, NAMES)
    assert [i.text for i in issues if i.code == "undeclared_fact"] == ["900"]


# --- claims must be supported by the evidence THEY cite -----------------------------------------------

def test_a_number_in_a_claim_must_be_in_the_cited_evidence() -> None:
    line = "I noticed that Acme Optical operates 130 locations across Texas"
    issues = deterministic_check(draft(f"Hi Sam,\n{line}", Claim(line, ("F1",))), EV, APPROVED, NAMES)
    assert codes(issues) == ["unsupported_in_claim"] and issues[0].text == "130"


def test_a_number_cannot_be_smuggled_in_because_approved_text_contains_it() -> None:
    line = "I noticed that Acme Optical operates 25 locations"      # '25' is in the case study
    issues = deterministic_check(draft(f"Hi Sam,\n{line}", Claim(line, ("F1",))), EV, APPROVED, NAMES)
    assert "unsupported_in_claim" in codes(issues)


def test_a_claim_may_use_a_fact_from_another_item_only_if_it_cites_it() -> None:
    line = "I noticed that Acme Optical opened a Boise office in March"
    assert "unsupported_in_claim" in codes(
        deterministic_check(draft(f"Hi Sam,\n{line}", Claim(line, ("F1",))), EV, APPROVED, NAMES))
    assert deterministic_check(draft(f"Hi Sam,\n{line}", Claim(line, ("F2",))), EV, APPROVED, NAMES) == []
    assert deterministic_check(draft(f"Hi Sam,\n{line}", Claim(line, ("F1", "F2"))), EV, APPROVED,
                               NAMES) == []


def test_known_names_are_allowed_inside_claims() -> None:
    line = "I noticed that Sam Lee leads marketing"       # Sam Lee is the contact
    issues = deterministic_check(draft(f"Hi Sam,\n{line}", Claim(line, ("F1",))), EV, APPROVED, NAMES)
    assert "unsupported_in_claim" not in codes(issues)


def test_claim_structure_problems_fail() -> None:
    line = "I noticed that Acme Optical operates 120 locations across Texas"
    base = f"Hi Sam,\n{line}"
    assert "claim_no_evidence" in codes(
        deterministic_check(draft(base, Claim(line, ())), EV, APPROVED, NAMES))
    assert "claim_unknown_evidence" in codes(
        deterministic_check(draft(base, Claim(line, ("F99",))), EV, APPROVED, NAMES))
    assert "claim_not_in_text" in codes(
        deterministic_check(draft("Hi Sam, hello.", Claim(line, ("F1",))), EV, APPROVED, NAMES))


def test_an_old_signal_cannot_be_called_recent() -> None:
    line = "Congrats, Acme Optical is hiring a Fleet Lead"
    issues = deterministic_check(draft(f"Hi Sam,\n{line}", Claim(line, ("S1",))), EV, APPROVED, NAMES)
    assert "stale_as_recent" in codes(issues)
    plain = "Acme Optical is hiring a Fleet Lead"
    assert "stale_as_recent" not in codes(
        deterministic_check(draft(f"Hi Sam,\n{plain}", Claim(plain, ("S1",))), EV, APPROVED, NAMES))


# --- the verifier: missing, broken or out of budget means FAIL ------------------------------------------

class Verifier:
    def __init__(self, result=None, exc=None) -> None:
        self.result, self.exc, self.calls = result, exc, 0

    def verify_claims(self, d, e):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.result


def test_no_verifier_fails_closed() -> None:
    result = check_draft(GOOD, EV, APPROVED, NAMES, None)
    assert not result.passed and codes(result.issues) == ["verifier_missing"]
    assert result.verifier == "missing"


def test_verifier_error_fails_closed() -> None:
    result = check_draft(GOOD, EV, APPROVED, NAMES, Verifier(exc=LLMError("boom")))
    assert not result.passed and codes(result.issues) == ["verifier_error"]


def test_unexpected_verifier_exception_fails_closed() -> None:
    result = check_draft(GOOD, EV, APPROVED, NAMES, Verifier(exc=ValueError("bug")))
    assert not result.passed and result.verifier == "error"


def test_verifier_over_budget_fails_closed() -> None:
    result = check_draft(GOOD, EV, APPROVED, NAMES, Verifier(exc=SpendCapExceeded("cap")))
    assert not result.passed and codes(result.issues) == ["verifier_over_budget"]
    assert result.verifier == "over_budget"


def test_verifier_rejection_fails_with_its_reason() -> None:
    result = check_draft(GOOD, EV, APPROVED, NAMES, Verifier([ClaimVerdict(0, False, "exaggerated")]))
    assert not result.passed and codes(result.issues) == ["verifier_rejected"]
    assert "exaggerated" in result.issues[0].message


def test_a_claim_the_verifier_skipped_is_not_assumed_supported() -> None:
    result = check_draft(GOOD, EV, APPROVED, NAMES, Verifier([]))
    assert not result.passed and codes(result.issues) == ["verifier_error"]


def test_verifier_is_not_called_when_the_rules_already_failed() -> None:
    verifier = Verifier([ClaimVerdict(0, True)])
    bad = draft("Hi Sam,\nYou have 999 stores.\nWould a short call be useful?")
    result = check_draft(bad, EV, APPROVED, NAMES, verifier)
    assert not result.passed and verifier.calls == 0


def test_rules_passing_alone_is_not_enough_when_there_are_claims() -> None:
    assert deterministic_check(GOOD, EV, APPROVED, NAMES) == []
    assert check_draft(GOOD, EV, APPROVED, NAMES, None).passed is False


def test_a_draft_with_no_personal_claims_needs_no_verifier() -> None:
    plain = draft("Hi Sam, would a short call be useful?")
    assert check_draft(plain, EV, APPROVED, NAMES, None).passed
