from dataclasses import replace

import pytest

from revos.domain.enums import Tier
from revos.skills.scoring import (
    Nudge,
    apply_nudge,
    matches,
    score_lead,
    score_with_nudge,
)

# Hand-calculated against packs/franchise_brands/icp.yaml (weights 0.5 / 0.3 / 0.2, tiers 65/40/20).
HOT = {   # fit 30+20+15+10+20+10 = 105 -> capped 100; intent 40+20+15 = 75; engagement 25+25 = 50
    "location_count": 120, "is_franchise": True, "industry": "optical", "region": "US",
    "annual_ad_spend_usd": 600000, "contact_seniority": "vp",
    "requested_demo": True, "visited_pricing_page": True, "website_visits_30d": 5,
    "replied": True, "reply_sentiment": "positive",
}
MID = {   # fit 15+15+10 = 40; intent 15+15 = 30; engagement 0
    "location_count": 25, "is_franchise": False, "industry": "restaurants", "region": "US",
    "annual_ad_spend_usd": 100000, "contact_seniority": "manager",
    "website_visits_30d": 4, "local_marketing_pain_mentioned": True,
}
STRONG_COLD = {   # fit 105 -> 100; nothing else
    "location_count": 60, "is_franchise": True, "industry": "hospitality", "region": "FR",
    "annual_ad_spend_usd": 300000, "contact_seniority": "director",
}
EDGE_A = {**{k: HOT[k] for k in list(HOT)[:6]},   # fit 100, intent 40+10 = 50, engagement 0
          "requested_demo": True, "hiring_marketing_roles": True}

GOLDEN = [
    # name, facts, fit, intent, engagement, total, tier
    ("hot franchise lead", HOT, 100, 75, 50, 83, Tier.A),             # 82.5 rounds half up
    ("mid-market, some interest", MID, 40, 30, 0, 29, Tier.C),         # 20 + 9
    ("great fit, no signals", STRONG_COLD, 100, 0, 0, 50, Tier.B),
    ("exactly on the tier A threshold", EDGE_A, 100, 50, 0, 65, Tier.A),
    ("nothing known", {}, 0, 0, 0, 0, Tier.DISQUALIFIED),              # below the minimum of 20
]


@pytest.mark.parametrize("name,facts,fit,intent,eng,total,tier", GOLDEN,
                         ids=[g[0] for g in GOLDEN])
def test_golden_leads(pack, name, facts, fit, intent, eng, total, tier) -> None:
    result = score_lead(facts, pack.scoring)
    assert (result.fit, result.intent, result.engagement) == (fit, intent, eng)
    assert (result.total, result.tier) == (total, tier)
    assert result.disqualified_by == ()


DISQUALIFIED = [
    ("competitor", {**HOT, "is_competitor": True}, "competitor"),
    ("do not contact", {**HOT, "do_not_contact": True}, "do_not_contact"),
    ("agency", {**HOT, "is_agency": True}, "other_segment_agency"),
    ("single location", {**HOT, "location_count": 1}, "single_location"),
]


@pytest.mark.parametrize("name,facts,rule", DISQUALIFIED, ids=[d[0] for d in DISQUALIFIED])
def test_disqualifiers_win_over_a_high_score(pack, name, facts, rule) -> None:
    result = score_lead(facts, pack.scoring)
    assert result.tier is Tier.DISQUALIFIED and result.disqualified_by == (rule,)
    assert any(r.category == "disqualifier" and r.rule_id == rule for r in result.reasons)


def test_every_point_has_a_written_reason(pack) -> None:
    for _name, facts, fit, intent, eng, _total, _tier in GOLDEN:
        result = score_lead(facts, pack.scoring)
        for category, expected in (("fit", fit), ("intent", intent), ("engagement", eng)):
            reasons = result.reasons_for(category)
            assert sum(r.points for r in reasons) == expected, category
            assert all(r.text.strip() for r in reasons)


def test_the_written_reason_names_the_rules_and_values(pack) -> None:
    result = score_lead(HOT, pack.scoring)
    text = result.summary
    assert "Tier A: total 83/100" in text
    assert "+30 Operates 120 locations" in text
    assert "fit is capped at 100" in text and "-5" in text
    assert "Industry 'optical' is in the target list" in text
    assert "gives tier A" in text


def test_scoring_is_a_pure_function(pack) -> None:
    facts = dict(HOT)
    first, second = score_lead(facts, pack.scoring), score_lead(facts, pack.scoring)
    assert first == second and facts == HOT


def test_missing_or_mistyped_facts_never_match(pack) -> None:
    result = score_lead({"location_count": "lots", "is_franchise": "false", "region": None},
                        pack.scoring)
    assert result.fit == 0 and result.disqualified_by == ()


def test_weights_come_from_the_pack(pack) -> None:
    heavy_intent = pack.scoring.model_copy(update={"weights": pack.scoring.weights.model_copy(
        update={"fit": 0.0, "intent": 1.0, "engagement": 0.0})})
    assert score_lead(HOT, heavy_intent).total == 75
    assert score_lead(HOT, pack.scoring).total == 83


@pytest.mark.parametrize("op,value,fact,expected", [
    ("gte", 10, 10, True), ("gt", 10, 10, False), ("lte", 10, 10, True), ("lt", 10, 10, False),
    ("between", [10, 20], 20, True), ("between", [10, 20], 21, False),
    ("in", ["a", "b"], "a", True), ("not_in", ["a"], "b", True), ("not_in", ["a"], "a", False),
    ("eq", "x", "x", True), ("ne", "x", "y", True), ("truthy", None, "no", False),
    ("truthy", None, "yes", True), ("gte", 1, True, False),
    ("contains_any", ["a"], ["a", "z"], True), ("contains_any", ["a"], "a", False),
])
def test_condition_operators(pack, op, value, fact, expected) -> None:
    from revos.segments.schema import Condition
    assert matches(Condition(field="f", op=op, value=value), {"f": fact}) is expected


# --- the LLM nudge ---------------------------------------------------------------------------

def nudge(delta: int, **kw) -> Nudge:
    return Nudge(delta, kw.get("reason", "the website lists new openings"), kw.get("evidence", ("F1",)))


def test_nudge_moves_the_total_and_the_tier_inside_the_bound(pack) -> None:
    base = score_lead(EDGE_A, pack.scoring)                        # 65, tier A
    down = apply_nudge(base, nudge(-10), pack.scoring)
    assert (down.total, down.tier) == (55, Tier.B)
    assert down.reasons[-1].category == "nudge" and down.reasons[-1].points == -10
    assert "F1" in down.reasons[-1].text


@pytest.mark.parametrize("asked,applied", [(30, 10), (-99, -10), (11, 10), (3, 3)])
def test_nudge_is_clamped_to_plus_minus_ten(pack, asked, applied) -> None:
    base = score_lead(STRONG_COLD, pack.scoring)                   # 50
    assert apply_nudge(base, nudge(asked), pack.scoring).total == 50 + applied


def test_pack_can_lower_the_limit_but_never_raise_it(pack) -> None:
    tight = pack.scoring.model_copy(update={"nudge_limit": 3})
    assert apply_nudge(score_lead(STRONG_COLD, tight), nudge(10), tight).total == 53
    loose = pack.scoring.model_copy(update={"nudge_limit": 99})     # bypasses validation on purpose
    assert apply_nudge(score_lead(STRONG_COLD, loose), nudge(50), loose).total == 60


@pytest.mark.parametrize("facts,rule", [(d[1], d[2]) for d in DISQUALIFIED])
def test_nudge_can_never_override_a_disqualifier(pack, facts, rule) -> None:
    base = score_lead(facts, pack.scoring)
    boosted = apply_nudge(base, nudge(10), pack.scoring)
    assert boosted.tier is Tier.DISQUALIFIED and boosted.total == base.total
    assert boosted.disqualified_by == (rule,)
    assert "ignored" in boosted.reasons[-1].text


def test_a_lead_the_rules_rejected_on_score_stays_rejected(pack) -> None:
    base = score_lead({}, pack.scoring)
    assert apply_nudge(base, nudge(10), pack.scoring).tier is Tier.DISQUALIFIED


def test_nudge_without_reason_or_evidence_is_ignored(pack) -> None:
    base = score_lead(STRONG_COLD, pack.scoring)
    assert apply_nudge(base, Nudge(10, "vibes", ()), pack.scoring).total == base.total
    assert apply_nudge(base, Nudge(10, "  ", ("F1",)), pack.scoring).total == base.total
    assert apply_nudge(base, None, pack.scoring) is base


class FakeProvider:
    def __init__(self, nudge_: Nudge | None = None, boom: bool = False) -> None:
        self.nudge_, self.boom, self.calls = nudge_, boom, 0

    def propose(self, facts, result):
        self.calls += 1
        if self.boom:
            raise TimeoutError("llm down")
        return self.nudge_


def test_provider_is_not_even_asked_about_disqualified_leads(pack) -> None:
    provider = FakeProvider(nudge(10))
    result = score_with_nudge({**HOT, "is_competitor": True}, pack.scoring, provider)
    assert provider.calls == 0 and result.tier is Tier.DISQUALIFIED


def test_provider_nudge_is_applied_to_qualifying_leads(pack) -> None:
    provider = FakeProvider(nudge(-10))
    result = score_with_nudge(EDGE_A, pack.scoring, provider)
    assert provider.calls == 1 and result.total == 55


def test_llm_outage_falls_back_to_the_rules_score(pack) -> None:
    result = score_with_nudge(EDGE_A, pack.scoring, FakeProvider(boom=True))
    rules_only = score_lead(EDGE_A, pack.scoring)
    assert (result.total, result.tier) == (rules_only.total, rules_only.tier)
    assert "unavailable" in result.reasons[-1].text


def test_no_provider_means_rules_only(pack) -> None:
    assert score_with_nudge(HOT, pack.scoring) == score_lead(HOT, pack.scoring)
    assert replace(score_lead(HOT, pack.scoring)).total == 83
