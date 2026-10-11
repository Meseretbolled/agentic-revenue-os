from datetime import UTC, datetime, timedelta
from uuid import UUID

from revos.ports.transcripts import Transcript
from revos.skills.call_matching import DealFacts, match_call, short_id

A, B, C = (UUID(f"{i}0000000-0000-0000-0000-000000000000") for i in "abc")
WHEN = datetime(2026, 10, 15, 15, 0, tzinfo=UTC)


def t(title="x", participants=(), when=WHEN):
    return Transcript("t1", title, when, tuple(participants), ())


def deals():
    return [DealFacts(A, "Acme Optical", "sam@acme.example", WHEN),
            DealFacts(B, "Brightside Auto", "pat@bright.example", None),
            DealFacts(C, "Cedar Hotels", "lee+tag@gmail.com", WHEN + timedelta(days=3))]


def test_a_participant_email_matches_the_contact() -> None:
    m = match_call(t(participants=["SAM@acme.example", "jo@us.example"]), deals())
    assert (m.kind, m.deal_id, m.by) == ("matched", A, ("participant_email",))


def test_the_title_can_carry_the_short_id_or_the_company_name() -> None:
    assert match_call(t(f"Call {short_id(B)}"), deals()).deal_id == B
    assert match_call(t("Intro with cedar hotels"), deals()).deal_id == C


def test_the_meeting_time_window_matches_only_inside_the_window() -> None:
    near = match_call(t(when=WHEN + timedelta(hours=2, minutes=59)), deals())
    assert near.kind == "matched" and near.deal_id == A and near.by == ("meeting_time",)
    assert match_call(t(when=WHEN + timedelta(hours=3, minutes=1)), deals()).kind == "none"


def test_a_plus_addressed_contact_is_the_same_inbox_as_its_base_address() -> None:
    assert match_call(t(participants=["lee@gmail.com"]), deals()).deal_id == C


def test_a_later_rule_narrows_an_earlier_ambiguity_but_never_guesses() -> None:
    pair = [DealFacts(A, "Acme Optical", "x+1@gmail.com", None), DealFacts(B, "Brightside Auto", "x+2@gmail.com", None)]
    assert match_call(t(participants=["x@gmail.com"]), pair).kind == "ambiguous"
    narrowed = match_call(t("Brightside Auto chat", ["x@gmail.com"]), pair)
    assert narrowed.kind == "matched" and narrowed.deal_id == B and narrowed.by == ("participant_email", "title")
    both = match_call(t("Acme Optical and Brightside Auto", ["x@gmail.com"]), pair)
    assert both.kind == "ambiguous" and set(both.candidates) == {A, B}


def test_no_evidence_is_no_match_even_with_one_open_deal() -> None:
    assert match_call(t("hello", when=WHEN + timedelta(days=30)), deals()[:1]).kind == "none"
    assert match_call(t("hello"), []).kind == "none"


def test_an_exact_contact_email_beats_a_plus_variant_of_the_same_inbox() -> None:
    pair = [DealFacts(A, "Acme", "lee@gmail.com", None), DealFacts(B, "Bee", "lee+b@gmail.com", None)]
    m = match_call(t("hello", ["lee@gmail.com"], WHEN + timedelta(days=30)), pair)
    assert (m.kind, m.deal_id) == ("matched", A)
