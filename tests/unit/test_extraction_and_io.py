from datetime import date

import pytest

from revos.domain.stages import DealStage
from revos.segments.schema import Extractor
from revos.skills.extraction import extract_fields
from revos.skills.io import parse_budget_usd, render_io, validate_io

NUMBER = Extractor(field="location_count", kind="number", pattern=r"(\d[\d,]*)\s+locations")
KEYWORD = Extractor(field="industry", kind="keyword", map={"optical": ["eye-care"], "auto": ["dealership"]})
FLAG = Extractor(field="is_franchise", kind="flag", any=["franchise"])
TEXT = ("Acme runs eye-care practices. We operate 1,200 locations in Texas.\n"
        "Our franchise partners run local campaigns.")
TODAY = date(2026, 10, 12)


def test_extractors_pull_values_with_the_sentence_they_came_from() -> None:
    found = {e.field: e for e in extract_fields(TEXT, [NUMBER, KEYWORD, FLAG])}
    assert found["location_count"].value == 1200
    assert found["location_count"].quote == "We operate 1,200 locations in Texas."
    assert (found["industry"].value, found["industry"].quote) == ("optical", "Acme runs eye-care practices.")
    assert found["is_franchise"].value is True and found["is_franchise"].quote.startswith("Our franchise")


def test_a_field_with_no_match_is_simply_absent() -> None:
    assert extract_fields("Nothing useful here.", [NUMBER, KEYWORD, FLAG]) == []


def test_the_first_matching_sentence_wins_and_every_quote_is_in_the_text() -> None:
    found = extract_fields("We operate 5 locations. We operate 9 locations.", [NUMBER])
    assert found[0].value == 5
    assert all(e.quote in TEXT for e in extract_fields(TEXT, [NUMBER, KEYWORD, FLAG]))


def test_extractor_shapes_are_validated() -> None:
    with pytest.raises(ValueError):
        Extractor(field="x", kind="number", pattern=r"(\d+)\s+(\w+)")      # two groups
    with pytest.raises(ValueError):
        Extractor(field="x", kind="number", pattern="(unclosed")
    with pytest.raises(ValueError):
        Extractor(field="x", kind="keyword")
    with pytest.raises(ValueError):
        Extractor(field="x", kind="flag", any=[])


BASE = {"budget": "Our budget is about $50,000 for the first quarter.", "timeline": "Q1",
        "decision_maker": "Dana", "needs": "local",
        "signer_name": "Priya Nair", "signer_role": "Head of Finance", "signer_email": "signer@fictional.example",
        "io_start_date": "2026-11-02", "io_end_date": "2027-02-01",
        "io_items": [{"id": "multi_location", "quantity": 120}, {"id": "creative_automation", "quantity": 3}]}


def check(pack, **change):
    return validate_io(pack, {**BASE, **change}, today=TODAY)


def test_a_complete_io_validates_and_totals_from_the_sample_rate_card(pack) -> None:
    result = check(pack)
    assert result.ok and result.total == 40 * 120 + 2500 * 3 == 12300
    assert [line["amount"] for line in result.line_items] == [4800, 7500]


def test_a_discount_up_to_the_maximum_is_fine_and_reduces_the_total(pack) -> None:
    assert check(pack, discount_pct=10).total == 11070
    assert check(pack, discount_pct=15).ok                       # the maximum itself is allowed


@pytest.mark.parametrize("change,problem", [
    ({"discount_pct": 15.5}, "above the maximum of 15"),
    ({"discount_pct": -1}, "discount_pct must be a number"),
    ({"io_items": []}, "no IO line items"),
    ({"io_items": [{"id": "gold_plated", "quantity": 1}]}, "not on the rate card"),
    ({"io_items": [{"id": "multi_location", "quantity": 0}]}, "positive quantity"),
    ({"io_items": [{"id": "multi_location", "quantity": True}]}, "positive quantity"),
    ({"io_items": [{"id": "multi_location", "quantity": 2, "unit_price": 35}]}, "does not match the rate card"),
    ({"budget": ""}, "missing deal field: budget"),
    ({"signer_name": ""}, "missing deal field: signer_name"),
    ({"signer_role": None}, "missing deal field: signer_role"),
    ({"signer_email": ""}, "missing deal field: signer_email"),
    ({"signer_email": "not-an-address"}, "signer_email is not an email address"),
    ({"budget": "We have not decided yet."}, "no dollar amount could be read"),
    ({"budget": "Our budget is about $400."}, "outside the pack's bounds"),
    ({"budget": "Our budget is about ten million dollars."}, "outside the pack's bounds"),
    ({"io_end_date": "2029-01-01"}, "longer than the maximum of 24 months"),
    ({"io_start_date": ""}, "missing deal field: io_start_date"),
    ({"io_start_date": "11/02/2026"}, "io_start_date is not a valid date"),
    ({"io_end_date": "2027-02-30"}, "io_end_date is not a valid date"),
    ({"io_start_date": "2026-10-01"}, "io_start_date is in the past"),
    ({"io_end_date": "2026-11-02"}, "io_end_date must be after io_start_date"),
    ({"io_end_date": "2026-10-30"}, "io_end_date must be after io_start_date"),
])
def test_each_invalid_input_fails_with_its_reason(pack, change, problem) -> None:
    result = check(pack, **change)
    assert not result.ok and any(problem in p for p in result.problems), result.problems


def test_a_matching_quoted_price_is_accepted(pack) -> None:
    assert check(pack, io_items=[{"id": "multi_location", "quantity": 2, "unit_price": 40}]).ok


def test_several_problems_are_all_reported(pack) -> None:
    result = check(pack, signer_name="", discount_pct=50, io_end_date="nonsense")
    assert len(result.problems) == 3 and result.hard_block


def test_an_over_maximum_discount_is_a_hard_block_that_nothing_can_lift(pack) -> None:
    over = check(pack, discount_pct=40)
    assert not over.ok and over.hard_block and len(over.problems) == 1 and "HARD BLOCK" in over.problems[0]
    for approval in ({"approved_by": "mgr", "discount_pct": 40}, {"approved_by": "ceo", "discount_pct": 99}):
        assert not check(pack, discount_pct=40, manager_approval=approval).ok          # a stored approval is ignored
    assert check(pack, discount_pct=15).ok and not check(pack, discount_pct=15).hard_block


def test_the_signer_address_must_be_on_the_allowlist_when_one_applies(pack) -> None:
    allowed = {"signer@fictional.example"}
    assert validate_io(pack, BASE, today=TODAY, signer_allowed=lambda e: e in allowed).ok
    blocked = validate_io(pack, {**BASE, "signer_email": "x@evil.org"}, today=TODAY,
                          signer_allowed=lambda e: e in allowed)
    assert not blocked.ok and "not on the recipient allowlist" in blocked.problems[0]


@pytest.mark.parametrize("text,amount", [
    ("Our budget is about $40,000 a month across all locations.", 40000),
    ("about 50k for the quarter", 50000), ("$1.5m this year", 1_500_000),
    ("Our budget is about two hundred thousand dollars for the first year.", 200000),
    ("fifty thousand dollars", 50000), ("one million dollars", 1_000_000),
    ("no number here", None), ("", None), (None, None)])
def test_a_budget_amount_is_read_from_the_quote(text, amount) -> None:
    assert parse_budget_usd(text) == amount


def test_the_validation_reports_the_budget_and_the_term(pack) -> None:
    result = check(pack)
    assert result.budget_usd == 50000 and result.term_months == 3.0 and "SAMPLE" in result.payment_terms


def test_the_rendered_io_carries_the_sample_label_the_terms_and_the_signer(pack) -> None:
    fields = {**BASE, "discount_pct": 0}
    text = render_io(pack, "Acme", validate_io(pack, fields, today=TODAY), fields, "IO-1", "2026-10-12")
    assert "SAMPLE" in text and "Acme" in text and "Total: 12300.0" in text
    assert "DCO and Creative Automation" in text and "2026-11-02 to 2027-02-01" in text
    assert "Priya Nair, Head of Finance" in text and "signer@fictional.example" in text
    assert "Payment terms:" in text and "50,000" in text


def test_exit_criteria_for_io_come_from_the_pack(pack) -> None:
    assert set(pack.stages.exit_criteria[DealStage.QUALIFIED]) <= set(BASE)
