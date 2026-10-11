"""Every segment pack, by configuration only: each one scores its fictional seed leads into the promised tiers with
written reasons, drafts a first touch that passes the claim checker with figures only from its own library, and
takes a deal all the way to closed won offline. The tests never name a segment: they run for every pack that has
seed profiles, so a new pack is covered the moment it is added."""

import json
import random
import re
from pathlib import Path

import pytest

from revos.agents.common import lead_facts
from revos.domain.stages import DealStage
from revos.runtime.worker import Worker
from revos.seed import load_profiles
from revos.segments import load_all
from revos.skills.asks import asks_in
from revos.skills.scoring import score_lead
from tests.conftest import PACKS_DIR
from tests.integration.test_close_the_deal import drain, qualified, send_all, stage
from tests.integration.test_io_auto import lead_record
from tests.pipeline_helpers import make_rig

PACKS = load_all(PACKS_DIR)
WITH_PROFILES: dict[str, dict] = {}
for _pid in PACKS:
    try:
        WITH_PROFILES[_pid] = load_profiles(_pid)
    except ValueError:
        pass
IDS = sorted(WITH_PROFILES)
TIER_ORDER = {"hot": "A", "warm": "B", "cold": "C"}


def cases():
    return [(pid, name) for pid in IDS for name in sorted(WITH_PROFILES[pid])]


def rig_for(conn, ro_conn, pack, clock, name, **extra):
    profile = WITH_PROFILES[pack.id][name]
    facts = {}
    for n, note in enumerate(profile["research_notes"], 1):          # what `revos seed` stores as sourced facts
        facts[f"fact:DEMO{n}"], facts[f"fact_source:DEMO{n}"] = note["text"], note["source"]
    fields = {**profile["fields"], **facts, **extra,
              **({"signer_name": profile["signer_name"], "signer_role": profile["signer_role"]}
                 if profile.get("signer_name") else {})}
    return make_rig(conn, ro_conn, pack, clock, segment=pack.id, domain=profile["domain"],
                    company=profile["company"], name=profile["contact"], fields=json.dumps(fields))


# --- the packs and their fixtures -----------------------------------------------------------------------------------

def test_there_is_more_than_one_segment_and_each_has_its_own_seed_leads():
    assert len(IDS) >= 2 and set(IDS) == set(PACKS)


@pytest.mark.parametrize("pid,name", cases())
def test_a_seed_profile_is_fictional_and_marked_so(pid, name):
    profile = WITH_PROFILES[pid][name]
    assert profile["research_notes"] and all(n["source"].startswith("fictional demo profile")
                                             for n in profile["research_notes"])
    assert profile["domain"].endswith(".example")


@pytest.mark.parametrize("pid,name", cases())
def test_a_seed_profile_scores_into_its_promised_tier_with_a_written_reason_per_rule(pid, name):
    pack = PACKS[pid]
    result = score_lead(lead_facts(WITH_PROFILES[pid][name]["fields"], pack), pack.scoring)
    assert result.tier.value == WITH_PROFILES[pid][name]["expect_tier"]
    assert result.reasons and all(r.text.strip() and "{" not in r.text for r in result.reasons)
    print(f"\nSCORE {pid}/{name}: {result.total}/100 tier {result.tier.value}\n{result.summary}")


@pytest.mark.parametrize("pid", IDS)
def test_the_hot_lead_beats_the_warm_one_in_every_pack(pid):
    pack, profiles = PACKS[pid], WITH_PROFILES[pid]
    totals = {n: score_lead(lead_facts(p["fields"], pack), pack.scoring).total for n, p in profiles.items()}
    ordered = [totals[n] for n in ("hot", "warm", "cold") if n in totals]
    assert ordered == sorted(ordered, reverse=True) and len(ordered) >= 2


def test_a_lead_that_belongs_to_another_segment_is_disqualified_by_the_rule_that_says_so():
    """An agency scored in a pack whose rules treat agencies as a different segment is disqualified, with the reason."""
    for pid, pack in PACKS.items():
        routing = [d for d in pack.scoring.disqualifiers if d.field == "is_agency" and d.op == "truthy"]
        if not routing:
            continue
        for other, profiles in WITH_PROFILES.items():
            for profile in profiles.values():
                if other != pid and profile["fields"].get("is_agency") is True:
                    result = score_lead({**profile["fields"]}, pack.scoring)
                    assert result.tier.value == "disqualified" and "different segment" in result.summary


# --- rate cards, messaging, case studies ---------------------------------------------------------------------------------

def test_every_rate_card_is_a_sample_with_its_own_packages_and_the_io_limits_are_the_same():
    cards = {pid: p.rate_card for pid, p in PACKS.items()}
    assert all("SAMPLE" in c.label and c.packages for c in cards.values())
    package_ids = [pk.id for c in cards.values() for pk in c.packages]
    assert len(package_ids) == len(set(package_ids))                       # different packages in each segment
    limits = {pid: (p.policy.io_budget_min_usd, p.policy.io_budget_max_usd, p.policy.io_max_term_months,
                    p.policy.max_discount_pct, p.policy.io_min_lead_days) for pid, p in PACKS.items()}
    assert len(set(limits.values())) == 1                                  # the same IO validation rules
    assert all(pk.min_monthly_budget_usd > 0 for c in cards.values() for pk in c.packages)


def test_no_pack_states_a_figure_in_its_wording():
    """Numbers come only from the case-study library: the messaging files hold none."""
    for pid in PACKS:
        text = (PACKS_DIR / pid / "messaging.yaml").read_text()
        wording = text[text.index("pillars:"):]                            # the wording, not the source note
        wording = "\n".join(line for line in wording.splitlines() if not line.lstrip().startswith("#"))
        assert not re.search(r"\d", wording.replace("{{", "").replace("}}", "")), pid


def test_a_case_study_used_in_two_packs_is_the_same_verified_record_in_both():
    seen: dict[str, tuple] = {}
    for pack in PACKS.values():
        for study in pack.case_studies:
            record = (study.customer, study.summary, tuple((m.label, m.value) for m in study.metrics),
                      study.source_url, study.label, study.source_verified, tuple(study.tags))
            assert study.source_verified is True
            assert seen.setdefault(study.id, record) == record, f"{study.id} differs between packs"


def test_every_pack_template_a_model_fills_asks_exactly_one_thing_by_itself():
    import jinja2
    env = jinja2.Environment(undefined=jinja2.Undefined)
    for pack in PACKS.values():
        ctx = {"first_name": "Maya", "company": "Acme", "personalised_line": "Acme runs many sites.",
               "pillar": pack.messaging.pillars[0], "case_study": None, "sender_name": "Jo",
               "unsubscribe_line": "U", "postal_address": "P", "slots_intro": "Here are a few times that work:",
               "slots_text": "- Thursday 09:00"}
        for name in ("first_touch", "follow_up", "meeting_offer"):
            body = env.from_string(pack.messaging.templates[name].body).render(ctx)
            assert len(asks_in(body, footer=("U", "P"))) == 1, (pack.id, name)


def test_objection_replies_and_pillars_promise_nothing_they_have_not_got():
    """The objection wording hedges: no promise of a margin, a result or a capability."""
    banned = re.compile(r"\b(guarantee|will (?:improve|increase|raise|boost|cut|save)|fully white-label|"
                        r"improves? your margin|increases? your margin)\b", re.IGNORECASE)
    for pack in PACKS.values():
        texts = [p.body for p in pack.messaging.pillars] + [t.body for t in pack.messaging.templates.values()]
        assert not [t for t in texts if banned.search(t)], pack.id


# --- a first touch and a whole deal, offline --------------------------------------------------------------------------------

@pytest.mark.parametrize("pid", IDS)
def test_a_first_touch_for_the_hot_lead_passes_the_claim_check_with_figures_only_from_its_own_library(
        conn, ro_conn, clock, pid):
    pack = PACKS[pid]
    rig = rig_for(conn, ro_conn, pack, clock, "hot")
    result = rig.orch.handle_lead_created(rig.deal_id)
    assert result.status == "action_proposed", result.reason
    payload = conn.execute("SELECT payload FROM actions WHERE type = 'first_touch'").fetchone()[0]
    template = pack.messaging.templates["first_touch"]
    assert payload["subject"] == re.sub(r"\{\{ company \}\}", WITH_PROFILES[pid]["hot"]["company"], template.subject)
    assert payload["draft_source"] != "template" and "fictional demo profile" in payload["body"]
    assert len(asks_in(payload["body"], footer=("To stop receiving", "1 Example Street"))) == 1
    library = {c.id: c for c in pack.case_studies}
    if payload["case_study_id"]:
        study = library[payload["case_study_id"]]
        assert study.metric_text in payload["body"] and study.source_url in payload["body"]
    others = {m.value for c in PACKS.values() for s in c.case_studies for m in s.metrics} - {
        m.value for s in pack.case_studies for m in s.metrics}
    assert not [v for v in others if v in payload["body"]]                  # nothing from a study this pack lacks
    print(f"\nFIRST TOUCH ({pid}):\nSubject: {payload['subject']}\n{payload['body']}")


@pytest.mark.parametrize("pid", IDS)
def test_a_deal_runs_to_closed_won_offline_and_nothing_is_really_sent(conn, ro_conn, clock, pid):
    pack = PACKS[pid]
    name = "warm"                                  # a tier whose first touch needs no veto window
    rig = qualified(conn, ro_conn, pack, clock, segment=pack.id, domain=WITH_PROFILES[pid][name]["domain"],
                    company=WITH_PROFILES[pid][name]["company"], name=WITH_PROFILES[pid][name]["contact"],
                    fields=json.dumps({**WITH_PROFILES[pid][name]["fields"], "signer_name": "Pat Doe",
                                       "signer_role": "Managing Director"}))
    assert type(rig.world.mailer).__name__ == "MockMailer" and type(rig.world.esign).__name__ == "MockESign"
    lead_record(conn)
    result = rig.orch.handle_io(rig.deal_id)                    # the terms are proposed from the call and the pack
    assert result.status == "action_proposed", result.reason
    fields = conn.execute("SELECT fields FROM deals").fetchone()[0]
    assert fields["io_terms_source"] == "auto" and fields["io_package"] in {p.id for p in pack.rate_card.packages}
    check = conn.execute("SELECT validation FROM documents").fetchone()[0]
    assert check["ok"] is True and check["package"] == fields["io_package"]
    send_all(rig)
    assert stage(conn) == "out_for_signature" and rig.world.esign.requests[0].test_mode is True

    envelope = fields_envelope = conn.execute("SELECT fields->>'envelope_id' FROM deals").fetchone()[0]
    worker = Worker(rig.orch.handlers(), clock, rng=random.Random(1))
    drain(worker, conn)
    rig.world.esign.simulate_signed(envelope)
    assert rig.orch.handle_signature_webhook(rig.world.esign.signed_event(envelope)) == "queued"
    drain(worker, conn)
    path = [r[0] for r in conn.execute("SELECT detail->>'to' FROM audit_log WHERE event = 'deal.stage_changed' "
                                       "ORDER BY id")]
    assert DealStage.CLOSED_WON.value in path and stage(conn) == "handoff" and fields_envelope == envelope
    assert path[:3] == ["enriched", "scored", "outreach"] and "meeting" in path and "qualified" in path
    assert rig.world.chat.posts and Path(PACKS_DIR / pid / "pack.yaml").exists()
    print(f"\nDEAL ({pid}): " + " -> ".join(["new", *path]))
