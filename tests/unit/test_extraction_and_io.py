from datetime import date

import pytest

from revos.domain.stages import DealStage
from revos.segments.schema import Extractor
from revos.skills.extraction import extract_fields
from revos.skills.io import manager_ceiling, render_io, validate_io

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


BASE = {"budget": "50k", "timeline": "Q1", "decision_maker": "Dana", "needs": "local",
        "signer_name": "Priya Nair", "signer_role": "Head of Finance",
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
    assert len(result.problems) == 3 and result.needs_manager_approval


def test_an_over_maximum_discount_needs_a_managers_approval_to_pass(pack) -> None:
    over = check(pack, discount_pct=40)
    assert not over.ok and over.needs_manager_approval and len(over.problems) == 1
    approved = check(pack, discount_pct=40, manager_approval={"approved_by": "mgr", "discount_pct": 40})
    assert approved.ok and not approved.needs_manager_approval
    too_small = check(pack, discount_pct=40, manager_approval={"approved_by": "mgr", "discount_pct": 25})
    assert not too_small.ok
    anonymous = check(pack, discount_pct=40, manager_approval={"discount_pct": 40})
    assert not anonymous.ok and manager_ceiling({"manager_approval": {"discount_pct": 40}}) == 0.0
    assert manager_ceiling({"manager_approval": {"approved_by": "m", "discount_pct": 30}}) == 30.0


def test_the_rendered_io_carries_the_sample_label_the_terms_and_the_signer(pack) -> None:
    fields = {**BASE, "discount_pct": 0}
    text = render_io(pack, "Acme", validate_io(pack, fields, today=TODAY), fields, "IO-1", "2026-10-12")
    assert "SAMPLE" in text and "Acme" in text and "Total: 12300.0" in text
    assert "DCO and Creative Automation" in text and "2026-11-02 to 2027-02-01" in text
    assert "Priya Nair, Head of Finance" in text


def test_exit_criteria_for_io_come_from_the_pack(pack) -> None:
    assert set(pack.stages.exit_criteria[DealStage.QUALIFIED]) <= set(BASE)
