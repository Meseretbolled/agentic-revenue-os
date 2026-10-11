"""The four published case studies: which one each situation picks, and that only their listed figures can be quoted."""

import json
import re

import pytest

from revos.agents.common import check_inputs, match_tags
from revos.agents.types import Identity
from revos.domain.drafting import Draft, Evidence
from revos.seed import load_profiles
from revos.skills.claims import deterministic_check
from tests.conftest import PACK_ID
from tests.mcp_helpers import call, data, make_ctx
from tests.pipeline_helpers import make_rig

GBB, ETHOS, LUXURY, PEARLE = "global_beauty_brand", "ethos_veterinary_health", "luxury_retail_brand", "pearle_vision"

LISTED = {
    GBB: {"$15.52", "$3.80", "$19.23", "$87.64"},
    ETHOS: {"267%", "289%", "2.6", "26%", "140"},
    LUXURY: {"29%", "24%", "39%"},
    PEARLE: set(),
}


def pick(ro_conn, pack, deal_id, **args):
    ctx, _ = make_ctx(ro_conn, pack, deal_id)
    return data(call(ctx, "reply", "casestudies.pick", **args))["case_study"]


def hot_rig(conn, ro_conn, pack, clock, **extra):
    profile = load_profiles(PACK_ID)["hot"]
    fields = {**profile["fields"], **extra}
    return make_rig(conn, ro_conn, pack, clock, domain=profile["domain"], company=profile["company"],
                    name=profile["contact"], fields=json.dumps(fields))


# --- the library itself --------------------------------------------------------------------------------------------

def test_the_library_holds_exactly_the_four_studies_with_their_urls_tags_and_labels(pack):
    by_id = {c.id: c for c in pack.case_studies}
    assert set(by_id) == {GBB, ETHOS, LUXURY, PEARLE}
    assert by_id[GBB].source_url == "https://cool.co/cool-ai/" and by_id[GBB].tags == ["price", "cpm", "performance"]
    assert by_id[ETHOS].source_url.endswith("NVA_Ethos_Veterinary_COOL_AI-case-study-insights.pdf")
    assert by_id[ETHOS].tags == ["franchise", "multi_location", "trust"]
    assert by_id[LUXURY].source_url == "https://cool.co/blog/case-study/luxury-retail-brand/"
    assert by_id[LUXURY].tags == ["trust", "incrementality"]
    assert by_id[PEARLE].source_url == "https://cool.co/" and by_id[PEARLE].tags == ["franchise", "general"]
    assert {c.id for c in pack.case_studies if c.label == "vendor-reported"} == {GBB, ETHOS, LUXURY}
    assert by_id[PEARLE].label == "public testimonial"
    assert all(c.source_verified for c in pack.case_studies)
    assert not any(c.source_url == "https://cool.co/" for c in pack.case_studies if c.id != PEARLE)


def test_each_study_holds_only_its_listed_figures_and_no_duration(pack):
    for study in pack.case_studies:
        text = f"{study.summary} {study.metric_text}"
        numbers = set(re.findall(r"\$?\d[\d,.]*\+?%?", text))
        cleaned = {n.rstrip("+").lstrip("+") for n in numbers}
        assert {n.rstrip(".") for n in cleaned} <= LISTED[study.id] | {""}, (study.id, cleaned)
        assert not re.search(r"\b(day|week|month|year|quarter)s?\b", text, re.I) or study.id == LUXURY, study.id
    gbb = next(c for c in pack.case_studies if c.id == GBB)
    assert "339" not in str(gbb) and "79%" not in str(gbb)
    luxury = next(c for c in pack.case_studies if c.id == LUXURY)
    assert "Daily" in luxury.metric_text and not re.search(r"\d+\s*(days?|weeks?|months?|years?)", luxury.metric_text)


# --- which study each situation picks ----------------------------------------------------------------------------------

@pytest.mark.parametrize("args,expected", [
    ({"situation": "price_objection", "tags": ["price", "cpm", "performance"]}, GBB),
    ({"situation": "price_objection", "tags": ["optical", "franchise", "price", "cpm", "performance"]}, GBB),
    ({"situation": "first_touch", "tags": ["franchise", "multi_location", "general"]}, ETHOS),
    ({"situation": "first_touch", "tags": ["multi_location", "general"]}, ETHOS),
    ({"situation": "first_touch", "tags": ["franchise", "general"]}, PEARLE),
    ({"situation": "first_touch", "tags": ["general"]}, PEARLE),
    ({"situation": "first_touch", "tags": ["trust", "incrementality", "general"]}, LUXURY),
    ({"situation": "first_touch", "tags": ["incrementality", "general"]}, LUXURY),
])
def test_the_study_whose_tags_match_the_situation_is_picked(conn, ro_conn, pack, clock, args, expected):
    rig = make_rig(conn, ro_conn, pack, clock)
    assert pick(ro_conn, pack, rig.deal_id, **args)["id"] == expected


def test_a_price_objection_only_ever_gets_the_study_made_for_it(conn, ro_conn, pack, clock):
    rig = make_rig(conn, ro_conn, pack, clock)
    assert pick(ro_conn, pack, rig.deal_id, situation="price_objection", tags=["franchise", "trust"])["id"] == GBB
    assert pick(ro_conn, pack, rig.deal_id, situation="price_objection", tags=["franchise"])["id"] == GBB


@pytest.mark.parametrize("fields,expected", [
    ({"is_franchise": True, "location_count": 85}, ["franchise", "multi_location"]),
    ({"location_count": 12}, ["multi_location"]),
    ({"location_count": 1}, []),
    ({"needs": "We cannot tell which campaigns drive incremental sales."}, ["trust", "incrementality"]),
    ({"objections": "Attribution worries us."}, ["trust", "incrementality"]),
])
def test_the_lead_tags_come_from_its_own_facts(fields, expected):
    tags = match_tags(fields)
    assert all(t in tags for t in expected)
    if not expected:
        assert "multi_location" not in tags and "trust" not in tags


def test_a_first_touch_for_a_multi_location_franchise_lead_uses_the_ethos_study(conn, ro_conn, pack, clock):
    rig = hot_rig(conn, ro_conn, pack, clock)
    rig.orch.handle_lead_created(rig.deal_id)
    payload = conn.execute("SELECT payload FROM actions WHERE type = 'first_touch'").fetchone()[0]
    assert payload["case_study_id"] == ETHOS
    assert "Ethos Veterinary Health" in payload["body"] and "vendor-reported" in payload["body"]
    assert "+267%" in payload["body"] and "140+" in payload["body"]
    assert "https://cool.co/wp-content/uploads/2026/06/NVA_Ethos" in payload["body"]
    assert "339" not in payload["body"] and "15.52" not in payload["body"]


def test_the_tags_of_a_lead_worried_about_attribution_pick_the_luxury_study(conn, ro_conn, pack, clock):
    """Such a lead without franchise or multi-location facts would be disqualified by the scoring rules before a
    first touch, so the mapping is checked from the lead's own facts to the picker."""
    rig = make_rig(conn, ro_conn, pack, clock)
    tags = [*match_tags({"needs": "We cannot tell which campaigns are incremental."}), "general"]
    assert pick(ro_conn, pack, rig.deal_id, situation="first_touch", tags=tags)["id"] == LUXURY


# --- only the listed figures can be quoted ------------------------------------------------------------------------------

def checked(pack, study_id, body):
    study = next((c for c in pack.case_studies if c.id == study_id), None)
    template = pack.messaging.templates["first_touch"]
    approved, names = check_inputs(pack, template, Identity(), "Acme", "Maya Ortiz", study, ())
    return deterministic_check(Draft("s", body, ()), Evidence(), approved, names)


def codes(issues) -> set[str]:
    return {i.code for i in issues}


@pytest.mark.parametrize("study_id,sentence", [
    (GBB, "Global Beauty Brand reported ROAS of $15.52 vs $3.80."),
    (GBB, "Global Beauty Brand reported CPA of $19.23 vs $87.64."),
    (ETHOS, "Ethos Veterinary Health reported +267% phone leads and +289% booked appointments."),
    (ETHOS, "Ethos Veterinary Health reported 2.6x geographic reach, CPA 26% lower, across 140+ locations."),
    (LUXURY, "Luxury Retail Brand reported daily revenue +29% and daily orders +24%."),
    (LUXURY, "Luxury Retail Brand reported 39% of conversions from net-new demand."),
])
def test_a_listed_figure_of_the_attached_study_is_allowed(pack, study_id, sentence):
    assert "undeclared_fact" not in codes(checked(pack, study_id, f"Hi Maya,\n\n{sentence}\n\nJo"))


@pytest.mark.parametrize("study_id,sentence,figure", [
    (GBB, "Global Beauty Brand saw a 339% increase.", "339%"),               # the page's figure we never use
    (GBB, "Global Beauty Brand cut costs by 79%.", "79%"),
    (GBB, "Global Beauty Brand reached ROAS of $16.00 vs $3.80.", "$16.00"),
    (ETHOS, "Ethos Veterinary Health grew phone leads +300%.", "300%"),
    (ETHOS, "Ethos Veterinary Health reached 3.1x reach.", "3.1"),
    (ETHOS, "Ethos Veterinary Health did it in 6 months.", "6"),             # no duration claim
    (LUXURY, "Luxury Retail Brand lifted revenue +35%.", "35%"),
    (LUXURY, "Luxury Retail Brand did it within 90 days.", "90"),
    (PEARLE, "Pearle Vision grew sales 40%.", "40%"),                         # a testimonial has no numbers
])
def test_a_figure_that_is_not_in_the_list_is_rejected(pack, study_id, sentence, figure):
    found = checked(pack, study_id, f"Hi Maya,\n\n{sentence}\n\nJo")
    assert "undeclared_fact" in codes(found) and any(figure in i.message for i in found), found


def test_a_figure_from_another_study_is_rejected_when_that_study_is_not_attached(pack):
    found = checked(pack, GBB, "Hi Maya,\n\nA sister network saw +267% phone leads and ROAS of $15.52.\n\nJo")
    assert any("267%" in i.message for i in found)                           # Ethos's figure, Global Beauty attached
    found = checked(pack, ETHOS, "Hi Maya,\n\nOne brand reported ROAS of $15.52 vs $3.80.\n\nJo")
    assert any("$15.52" in i.message for i in found)
    found = checked(pack, None, "Hi Maya,\n\nOne network reported +267% phone leads.\n\nJo")
    assert any("267%" in i.message for i in found)                           # no study attached: no figures at all


def test_digits_in_a_link_neither_need_approval_nor_grant_it(pack):
    """The Ethos link contains /2026/06/: that must not make "6" or "2026" quotable."""
    assert any("2026/06" in c.source_url for c in pack.case_studies)
    found = checked(pack, ETHOS, "Hi Maya,\n\nThey did it in 6 months. Source: "
                    "https://cool.co/wp-content/uploads/2026/06/NVA_Ethos_Veterinary_COOL_AI-case-study-insights.pdf\n\nJo")
    assert [i.message for i in found if "2026" in i.message or "06" in i.message] == []
    assert any("'6'" in i.message for i in found)
