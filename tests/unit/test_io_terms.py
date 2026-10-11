from dataclasses import replace
from datetime import date

import pytest

from revos.skills.io import validate_io
from revos.skills.io_terms import monthly_budget, parse_deadline, propose_io_terms

TODAY = date(2026, 10, 12)                                       # a Monday
FIELDS = {"budget": "Our budget is about $40,000 a month across all locations.",
          "timeline": "within six weeks, we want each running before the holiday season starts.",
          "location_count": 85, "signer_name": "Marcus Reyes", "signer_role": "VP of Marketing",
          "signer_email": "signer@fictional.example", "decision_maker": "Marcus", "needs": "creative"}


def fields(**change):
    return {**FIELDS, **change}


@pytest.mark.parametrize("text,monthly,period", [
    ("Our budget is about $40,000 a month across all locations.", 40000, "month"),
    ("about $2,000 per month", 2000, "month"), ("$5,000 monthly", 5000, "month"),
    ("about fifty thousand dollars for the first quarter", 50000 / 3, "quarter"),
    ("Our budget is about two hundred thousand dollars for the first year.", 200000 / 12, "year"),
    ("$1.2m annual", 100000, "year"),
    ("Our budget is about $40,000.", None, ""),                    # no period: never guessed
    ("we have not decided", None, ""), ("", None, ""), (None, None, "")])
def test_the_budget_is_read_per_month_and_the_period_must_be_stated(text, monthly, period) -> None:
    got, got_period = monthly_budget(text)
    assert got_period == period and (got == pytest.approx(monthly) if monthly else got is None)


@pytest.mark.parametrize("text,days", [
    ("within six weeks, we want it running", 42), ("we want it live in 3 months", 90),
    ("within 8 weeks", 56), ("ideally within one month", 30)])
def test_a_deadline_is_read_from_the_timeline(text, days) -> None:
    from datetime import timedelta
    assert parse_deadline(text, TODAY) == TODAY + timedelta(days=days)


def test_a_quarter_deadline_and_missing_deadlines() -> None:
    assert parse_deadline("launch by Q1", TODAY) == date(2027, 3, 31)
    assert parse_deadline("sometime soon", TODAY) is None and parse_deadline(None, TODAY) is None


@pytest.mark.parametrize("budget,package", [
    ("$5,000 a month", "starter"), ("$14,999 a month", "starter"), ("$15,000 a month", "growth"),
    ("$34,999 a month", "growth"), ("$35,000 a month", "scale"), ("$40,000 a month", "scale"),
    ("$900,000 a month", "scale")])
def test_the_package_is_the_largest_one_the_budget_reaches(pack, budget, package) -> None:
    got = propose_io_terms(pack, fields(budget=budget), TODAY)
    assert got.ok and got.package_id == package, got.needs_a_person


def test_a_budget_below_the_smallest_package_needs_a_person(pack) -> None:
    got = propose_io_terms(pack, fields(budget="$4,999 a month"), TODAY)
    assert not got.ok and got.items == () and "below the smallest package" in got.needs_a_person[0]


def test_the_terms_follow_the_pack_and_the_call(pack) -> None:
    got = propose_io_terms(pack, fields(), TODAY)
    assert got.ok and got.package_id == "scale" and got.discount_pct == 0.0
    assert got.start == "2026-10-26" and got.end == "2027-01-26"          # a Monday, setup lead of 14 days, 3 months
    assert date.fromisoformat(got.start).weekday() == 0
    assert got.deadline == "2026-11-23" and got.monthly_budget_usd == 40000
    quantities = {i["id"]: i["quantity"] for i in got.items}
    assert quantities == {"creative_automation": 6, "multi_location": 255, "measurement": 3}   # x3 months


def test_a_quantity_follows_the_lead_field_up_to_the_packages_cap(pack) -> None:
    growth = fields(budget="$20,000 a month", location_count=85)
    assert {i["id"]: i["quantity"] for i in propose_io_terms(pack, growth, TODAY).items}["multi_location"] == 180
    small = fields(budget="$20,000 a month", location_count=12)
    assert {i["id"]: i["quantity"] for i in propose_io_terms(pack, small, TODAY).items}["multi_location"] == 36
    unknown = propose_io_terms(pack, {k: v for k, v in growth.items() if k != "location_count"}, TODAY)
    assert not unknown.ok and "location_count is not known" in unknown.needs_a_person[0]


def test_the_start_is_never_in_the_past_and_always_a_monday(pack) -> None:
    for offset in range(14):
        today = date(2026, 10, 12 + offset)
        got = propose_io_terms(pack, fields(), today)
        assert date.fromisoformat(got.start) >= today and date.fromisoformat(got.start).weekday() == 0
        assert (date.fromisoformat(got.start) - today).days >= pack.policy.io_min_lead_days


@pytest.mark.parametrize("change,fragment", [
    ({"budget": "Our budget is about $40,000."}, "no amount or no period"),
    ({"budget": ""}, "budget: not stated"), ({"budget": "we have not decided"}, "no amount or no period"),
    ({"timeline": ""}, "timeline: not stated"), ({"timeline": "sometime soon"}, "no deadline could be read"),
    ({"timeline": "within one week"}, "sooner than our 14-day setup allows")])
def test_a_term_that_cannot_be_set_within_the_packs_limits_needs_a_person(pack, change, fragment) -> None:
    got = propose_io_terms(pack, fields(**change), TODAY)
    assert not got.ok and any(fragment in reason for reason in got.needs_a_person), got.needs_a_person


def test_a_volume_discount_comes_only_from_the_packs_own_rules(pack) -> None:
    from revos.segments.schema import VolumeDiscount
    rules = pack.rate_card.model_copy(update={"volume_discounts": [
        VolumeDiscount(min_monthly_budget_usd=30000, discount_pct=5)]})
    with_rule = replace(pack, rate_card=rules)
    assert propose_io_terms(with_rule, fields(budget="$40,000 a month"), TODAY).discount_pct == 5
    assert propose_io_terms(with_rule, fields(budget="$20,000 a month"), TODAY).discount_pct == 0
    assert propose_io_terms(pack, fields(budget="$40,000 a month"), TODAY).discount_pct == 0     # no rule: none


def test_a_pack_rule_that_exceeds_the_maximum_discount_is_a_hard_block_not_a_proposal(pack) -> None:
    from revos.segments.schema import VolumeDiscount
    rules = pack.rate_card.model_copy(update={"volume_discounts": [
        VolumeDiscount(min_monthly_budget_usd=1000, discount_pct=pack.policy.max_discount_pct + 5)]})
    got = propose_io_terms(replace(pack, rate_card=rules), fields(), TODAY)
    assert not got.ok and "a hard block" in got.needs_a_person[-1]


def test_a_proposal_that_is_ok_passes_the_ios_own_validation(pack) -> None:
    got = propose_io_terms(pack, fields(), TODAY)
    check = validate_io(pack, {**fields(), "io_items": list(got.items), "io_start_date": got.start,
                               "io_end_date": got.end, "discount_pct": got.discount_pct}, today=TODAY)
    assert check.ok, check.problems
