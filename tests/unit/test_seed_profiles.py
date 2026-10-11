"""The hot, warm and cold seed leads score into the tiers the fixture promises, from the pack's own
weights, each with a written reason. The lead data is in YAML; no Python names a segment."""

import pytest

from revos.agents.common import lead_facts
from revos.seed import PROFILE_NAMES, load_profiles
from revos.skills.scoring import score_lead
from tests.conftest import PACK_ID

TIERS = {"hot": "A", "warm": "B", "cold": "C"}


@pytest.mark.parametrize("name", PROFILE_NAMES)
def test_each_profile_scores_into_its_tier_with_reasons_from_the_packs_rules(pack, name) -> None:
    profile = load_profiles(PACK_ID)[name]
    result = score_lead(lead_facts(profile["fields"], pack), pack.scoring)
    assert result.tier.value == TIERS[name] == profile["expect_tier"]
    assert result.reasons and all(r.text.strip() for r in result.reasons)
    print(name, result.fit, result.intent, result.engagement, result.total, result.tier.value)


def test_hot_is_strong_on_fit_and_intent_and_the_tiers_are_ordered(pack) -> None:
    results = {n: score_lead(lead_facts(load_profiles(PACK_ID)[n]["fields"], pack), pack.scoring)
               for n in PROFILE_NAMES}
    assert results["hot"].total > results["warm"].total > results["cold"].total >= pack.scoring.tiers.C
    assert results["hot"].fit >= 90 and results["hot"].intent >= 90


def test_no_profile_fakes_an_event_that_must_come_from_the_world() -> None:
    for profile in load_profiles(PACK_ID).values():
        assert not {"replied", "meeting_booked", "reply_sentiment"} & set(profile["fields"])


def test_a_broken_fixture_is_refused(tmp_path) -> None:
    bad = tmp_path / "p.yaml"
    bad.write_text("x:\n  hot: {company: A}\n")
    with pytest.raises(ValueError, match="lacks"):
        load_profiles("x", bad)
    with pytest.raises(ValueError, match="no seed profiles"):
        load_profiles("nope", bad)


@pytest.mark.parametrize("name", PROFILE_NAMES)
def test_the_pipeline_scores_each_seeded_profile_to_its_tier_and_stores_the_reason(
        conn, ro_conn, pack, clock, name) -> None:
    import json

    from tests.pipeline_helpers import make_rig
    profile = load_profiles(PACK_ID)[name]
    rig = make_rig(conn, ro_conn, pack, clock, domain=profile["domain"], company=profile["company"],
                   fields=json.dumps(profile["fields"]))
    rig.orch.handle_lead_created(rig.deal_id)
    total, tier, reason = conn.execute("SELECT total_score, tier, score_reason FROM deals").fetchone()
    assert tier == TIERS[name] and reason and len(reason) > 20
    print(f"\nPROFILE {name}: {total}/100 tier {tier}\n  reason: {reason}")
