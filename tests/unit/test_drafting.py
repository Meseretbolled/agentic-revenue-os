import pytest

from revos.adapters.fake_llm import FakeLLM
from revos.domain.drafting import Claim, Evidence, EvidenceItem
from revos.domain.errors import LLMError, SpendCapExceeded
from revos.ports.llm import DraftProposal
from revos.skills.drafting import draft_with_checks, render_email
from tests.helpers import first_touch_setup

GOOD_LINE = "I noticed that Acme Optical operates 120 locations"
BAD = DraftProposal("I noticed that Acme Optical operates 130 locations",
                    (Claim("I noticed that Acme Optical operates 130 locations", ("F1",)),))
GOOD = DraftProposal(GOOD_LINE, (Claim(GOOD_LINE, ("F1",)),))


def run(pack, llm, verifier="llm", **kw):
    request, approved, names = first_touch_setup(pack)
    return draft_with_checks(llm, request, approved, names, llm if verifier == "llm" else verifier,
                             **kw)


def test_default_fake_draft_passes_end_to_end(pack) -> None:
    llm = FakeLLM()
    out = run(pack, llm)
    assert out.status == "passed" and out.attempts == 1
    assert llm.calls == ["draft_email", "verify_claims"]
    assert "I noticed that Acme Optical operates 120 locations" in out.draft.body
    assert "Unsubscribe: reply STOP" in out.draft.body and "1 Example Street" in out.draft.body
    assert "$15.52 vs $3.80" in out.draft.body and "public testimonial" in out.draft.body


def test_the_model_writes_only_the_line_the_rest_is_the_approved_template(pack) -> None:
    request, _, _ = first_touch_setup(pack)
    draft = render_email(request, GOOD)
    assert draft.body.startswith("Hi Sam,") and "Would a short call be useful?" in draft.body
    assert draft.subject == "Scaling local campaigns at Acme Optical"


def test_a_bad_first_draft_is_regenerated_with_the_issues_as_feedback(pack) -> None:
    llm = FakeLLM(script=[BAD, GOOD])
    out = run(pack, llm)
    assert out.status == "passed" and out.attempts == 2
    assert llm.feedback_seen[0] == () and any("130" in m for m in llm.feedback_seen[1])


def test_regeneration_stops_after_two_retries_and_goes_to_a_person(pack) -> None:
    llm = FakeLLM(script=[BAD, BAD, BAD, GOOD])          # the good one would only come 4th
    out = run(pack, llm)
    assert out.status == "needs_human" and out.attempts == 3
    assert llm.calls.count("draft_email") == 3 and llm.calls.count("verify_claims") == 0
    assert {i.code for i in out.issues} == {"unsupported_in_claim"}


def test_a_failing_draft_is_handed_over_whole_nothing_is_deleted(pack) -> None:
    out = run(pack, FakeLLM(script=[BAD, BAD, BAD]))
    assert out.status == "needs_human"
    assert "130 locations" in out.draft.body                       # the bad sentence is still there
    assert "Would a short call be useful?" in out.draft.body       # and so is everything else


def test_max_regenerations_can_be_lowered(pack) -> None:
    llm = FakeLLM(script=[BAD, BAD, BAD])
    assert run(pack, llm, max_regenerations=0).attempts == 1
    assert llm.calls.count("draft_email") == 1


def test_missing_verifier_goes_to_a_person_without_wasting_regenerations(pack) -> None:
    llm = FakeLLM(script=[GOOD, GOOD, GOOD])
    out = run(pack, llm, verifier=None)
    assert out.status == "needs_human" and out.attempts == 1
    assert [i.code for i in out.issues] == ["verifier_missing"]
    assert llm.calls == ["draft_email"]


def test_verifier_error_goes_to_a_person(pack) -> None:
    llm = FakeLLM(script=[GOOD, GOOD], fail={"verify_claims": LLMError("down")})
    out = run(pack, llm)
    assert out.status == "needs_human" and out.attempts == 1
    assert out.issues[0].code == "verifier_error"


def test_spend_cap_on_the_verifier_goes_to_a_person(pack) -> None:
    llm = FakeLLM(script=[GOOD], fail={"verify_claims": SpendCapExceeded("cap")})
    out = run(pack, llm)
    assert out.status == "needs_human" and out.issues[0].code == "verifier_over_budget"
    assert llm.calls.count("draft_email") == 1


def test_spend_cap_on_the_drafter_goes_to_a_person_with_no_draft(pack) -> None:
    out = run(pack, FakeLLM(fail={"draft_email": SpendCapExceeded("cap")}))
    assert out.status == "needs_human" and out.draft is None
    assert out.issues[0].code == "llm_unavailable"


def test_rejection_by_the_verifier_is_retried_then_goes_to_a_person(pack) -> None:
    from revos.ports.llm import ClaimVerdict
    llm = FakeLLM(script=[GOOD, GOOD, GOOD], verdicts=[ClaimVerdict(0, False, "exaggerated")])
    out = run(pack, llm)
    assert out.status == "needs_human" and out.attempts == 3
    assert llm.calls.count("verify_claims") == 3


def test_missing_template_variable_is_not_papered_over(pack) -> None:
    request, approved, names = first_touch_setup(pack)
    broken = type(request)(request.subject_template, request.body_template,
                           {k: v for k, v in request.context.items() if k != "postal_address"},
                           request.evidence)
    out = draft_with_checks(FakeLLM(), broken, approved, names, FakeLLM())
    assert out.status == "needs_human" and "postal_address" in out.issues[0].message


def test_stale_evidence_cannot_be_presented_as_new(pack) -> None:
    ev = Evidence.of([EvidenceItem("S1", "Acme Optical is hiring a Fleet Lead", stale=True)])
    line = "Congrats, Acme Optical is hiring a Fleet Lead"
    llm = FakeLLM(script=[DraftProposal(line, (Claim(line, ("S1",)),))] * 3)
    request, approved, names = first_touch_setup(pack, evidence=ev)
    out = draft_with_checks(llm, request, approved, names, llm)
    assert out.status == "needs_human" and {i.code for i in out.issues} == {"stale_as_recent"}


@pytest.mark.parametrize("study", ["global_beauty_brand", "celebree", "air_france", None])
def test_every_library_case_study_renders_and_passes(pack, study) -> None:
    request, approved, names = first_touch_setup(pack, case_study_id=study)
    llm = FakeLLM()
    out = draft_with_checks(llm, request, approved, names, llm)
    assert out.status == "passed", out.issues
