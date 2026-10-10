import pytest

from revos.domain.stages import DealStage
from revos.segments.schema import Extractor
from revos.skills.extraction import extract_fields
from revos.skills.io import render_io, validate_io

NUMBER = Extractor(field="location_count", kind="number", pattern=r"(\d[\d,]*)\s+locations")
KEYWORD = Extractor(field="industry", kind="keyword", map={"optical": ["eye-care"], "auto": ["dealership"]})
FLAG = Extractor(field="is_franchise", kind="flag", any=["franchise"])
TEXT = ("Acme runs eye-care practices. We operate 1,200 locations in Texas.\n"
        "Our franchise partners run local campaigns.")


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
        "io_items": [{"id": "multi_location", "quantity": 120}, {"id": "creative_automation", "quantity": 3}]}


def test_a_complete_io_validates_and_totals_from_the_sample_rate_card(pack) -> None:
    check = validate_io(pack, BASE)
    assert check.ok and check.total == 40 * 120 + 2500 * 3 == 12300
    assert [line["amount"] for line in check.line_items] == [4800, 7500]


def test_a_discount_reduces_the_total(pack) -> None:
    check = validate_io(pack, {**BASE, "discount_pct": 10})
    assert check.ok and check.total == 11070


@pytest.mark.parametrize("change,problem", [
    ({"discount_pct": 15.5}, "above the maximum of 15"),
    ({"discount_pct": -1}, "discount_pct must be a number"),
    ({"io_items": []}, "no IO line items"),
    ({"io_items": [{"id": "gold_plated", "quantity": 1}]}, "not on the rate card"),
    ({"io_items": [{"id": "multi_location", "quantity": 0}]}, "positive quantity"),
    ({"io_items": [{"id": "multi_location", "quantity": True}]}, "positive quantity"),
    ({"budget": ""}, "missing deal field: budget"),
])
def test_invalid_io_inputs_are_reported(pack, change, problem) -> None:
    check = validate_io(pack, {**BASE, **change})
    assert not check.ok and any(problem in p for p in check.problems)


def test_the_rendered_io_carries_the_sample_label_and_the_figures(pack) -> None:
    check = validate_io(pack, BASE)
    text = render_io(pack, "Acme", check, 0, "IO-1", "2026-10-12")
    assert "SAMPLE" in text and "Acme" in text and "Total: 12300.0" in text
    assert "DCO and Creative Automation" in text


def test_exit_criteria_for_io_come_from_the_pack(pack) -> None:
    assert set(pack.stages.exit_criteria[DealStage.QUALIFIED]) <= set(BASE)
