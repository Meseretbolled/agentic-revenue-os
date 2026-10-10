import httpx
import pytest

from revos.adapters.hubspot.client import HubSpotClient, HubSpotError, HubSpotUnavailable
from revos.adapters.hubspot.crm import HubSpotCRM, build_deal_properties, note_token
from revos.adapters.hubspot.stages import DEFAULT_STAGE_MAP, hubspot_stage
from revos.domain.errors import PermanentError
from revos.domain.stages import DealStage
from tests.fakes.hubspot import TOKEN, FakeHubSpot


@pytest.fixture
def rig():
    fake = FakeHubSpot()
    slept: list[float] = []
    crm = HubSpotCRM(HubSpotClient(TOKEN, http=fake.client(), sleep=slept.append))
    return crm, fake, slept


def count(fake, kind) -> int:
    return len(fake.objects[kind])


def creates(fake, kind) -> int:
    return sum(1 for m, p in fake.calls if m == "POST" and p == f"/crm/v3/objects/{kind}")


# --- contacts and companies: search first, so a retry never duplicates ---------------------------

def test_a_contact_is_searched_for_before_it_is_created_and_flagged_as_demo(rig) -> None:
    crm, fake, _ = rig
    contact_id, created = crm.upsert_contact("Sam.Lee@Brightside.example", {"firstname": "Sam"}, "k1")
    assert created and count(fake, "contacts") == 1
    assert fake.calls[0][1] == "/crm/v3/objects/contacts/search"            # the search came first
    props = fake.objects["contacts"][contact_id]["properties"]
    assert props["email"] == "sam.lee@brightside.example" and props["revos_demo"] == "true"
    assert props["revos_key"] == "k1" and props["firstname"] == "Sam"


def test_upserting_the_same_contact_again_updates_it_and_never_duplicates(rig) -> None:
    crm, fake, _ = rig
    first, _ = crm.upsert_contact("sam@x.example", {"firstname": "Sam"}, "k1")
    second, created = crm.upsert_contact("SAM@x.example", {"lastname": "Lee"}, "k2")
    assert first == second and not created and count(fake, "contacts") == 1 and creates(fake, "contacts") == 1
    props = fake.objects["contacts"][first]["properties"]
    assert props["firstname"] == "Sam" and props["lastname"] == "Lee" and props["revos_key"] == "k1"


def test_companies_are_found_by_domain(rig) -> None:
    crm, fake, _ = rig
    a, _ = crm.upsert_company("Brightside-Optical.example", {"name": "Brightside"}, "c1")
    b, created = crm.upsert_company("brightside-optical.example", {"name": "Brightside Optical Group"}, "c2")
    assert a == b and not created and count(fake, "companies") == 1
    assert fake.objects["companies"][a]["properties"]["name"] == "Brightside Optical Group"
    assert fake.objects["companies"][a]["properties"]["revos_demo"] == "true"


def test_losing_a_create_race_falls_back_to_updating_the_winner(rig) -> None:
    crm, fake, _ = rig
    fake.objects["contacts"]["77"] = {"id": "77", "properties": {"email": "race@x.example"}}
    original = crm._find
    state = {"n": 0}

    def blind_first(kind, prop, value):          # the first search misses (index lag), as HubSpot's can
        state["n"] += 1
        return None if state["n"] == 1 else original(kind, prop, value)

    crm._find = blind_first
    contact_id, created = crm.upsert_contact("race@x.example", {"firstname": "Ray"}, "k9")
    assert contact_id == "77" and not created and count(fake, "contacts") == 1
    assert fake.objects["contacts"]["77"]["properties"]["firstname"] == "Ray"


# --- the response to a create is lost: look before trying again --------------------------------------

@pytest.mark.parametrize("failure", ["502", "lost"])
def test_a_create_whose_answer_was_lost_is_found_not_repeated(rig, failure) -> None:
    crm, fake, slept = rig

    def hook(kind):
        if failure == "502":
            return httpx.Response(502, json={})
        raise httpx.ReadError("connection dropped after HubSpot saved the record")

    state = {"armed": True}

    def once(kind):
        if state["armed"]:
            state["armed"] = False
            return hook(kind)
        return None

    fake.after_create = once
    contact_id, created = crm.upsert_contact("lost@x.example", {}, "k-lost")
    assert not created and count(fake, "contacts") == 1 and creates(fake, "contacts") == 1
    assert fake.objects["contacts"][contact_id]["properties"]["revos_key"] == "k-lost"


def test_a_create_that_really_failed_is_tried_again(rig) -> None:
    crm, fake, _ = rig
    fake.fail_create.append(502)                          # fails BEFORE saving: nothing to find
    contact_id, created = crm.upsert_contact("fresh@x.example", {}, "k-fresh")
    assert created and count(fake, "contacts") == 1 and creates(fake, "contacts") == 2


def test_after_three_failed_tries_it_gives_up_loudly(rig) -> None:
    crm, fake, _ = rig
    fake.fail_create.extend([502] * 20)
    with pytest.raises(HubSpotUnavailable):
        crm.upsert_contact("nope@x.example", {}, "k")
    assert count(fake, "contacts") == 0


# --- deals and the stage map ---------------------------------------------------------------------------

def test_a_deal_is_found_by_revos_deal_id_updated_not_duplicated_and_associated(rig) -> None:
    crm, fake, _ = rig
    contact, _ = crm.upsert_contact("sam@x.example", {}, "k1")
    company, _ = crm.upsert_company("x.example", {"name": "X"}, "k2")
    deal, created = crm.upsert_deal("deal-uuid-1", {"dealname": "X deal"}, "k3", stage=DealStage.SCORED,
                                    contact_id=contact, company_id=company)
    again, created_again = crm.upsert_deal("deal-uuid-1", {"amount": "100"}, "k4", stage=DealStage.MEETING)
    assert created and not created_again and deal == again and count(fake, "deals") == 1
    props = fake.objects["deals"][deal]["properties"]
    assert props["revos_deal_id"] == "deal-uuid-1" and props["revos_demo"] == "true"
    assert props["dealstage"] == "presentationscheduled" and props["revos_stage"] == "meeting"
    assert props["pipeline"] == "default" and props["amount"] == "100" and props["dealname"] == "X deal"
    assert ("deals", deal, "contacts", contact) in fake.associations
    assert ("deals", deal, "companies", company) in fake.associations


def test_every_stage_maps_to_a_stage_the_default_pipeline_has_and_keeps_its_exact_name(rig) -> None:
    crm, fake, _ = rig
    for stage in DealStage:
        assert hubspot_stage(stage) in fake.pipeline_stages, stage
    assert set(DEFAULT_STAGE_MAP) == set(DealStage)
    for i, stage in enumerate(DealStage):
        deal, _ = crm.upsert_deal(f"d{i}", {}, f"k{i}", stage=stage)
        props = fake.objects["deals"][deal]["properties"]
        assert props["revos_stage"] == stage.value and props["dealstage"] == DEFAULT_STAGE_MAP[stage]


def test_stages_that_share_a_hubspot_stage_stay_distinguishable(rig) -> None:
    crm, fake, _ = rig
    a, _ = crm.upsert_deal("a", {}, "ka", stage=DealStage.OUTREACH)
    b, _ = crm.upsert_deal("b", {}, "kb", stage=DealStage.REPLIED)
    pa, pb = (fake.objects["deals"][x]["properties"] for x in (a, b))
    assert pa["dealstage"] == pb["dealstage"] and pa["revos_stage"] != pb["revos_stage"]


def test_the_deal_properties_carry_score_tier_reason_call_fields_with_quotes_and_lessons() -> None:
    props = build_deal_properties(
        {"stage": "qualified", "total_score": 61, "tier": "B", "score_reason": "tier B because ..."},
        {"budget": "about fifty thousand", "quote:budget": "Our budget is about fifty thousand dollars.",
         "signer": "Priya Nair signs", "quote:signer": "Priya Nair, our Head of Finance, will sign."},
        [3, 1])
    assert props["revos_stage"] == "qualified" and props["revos_score"] == 61
    assert props["revos_tier"] == "B" and props["revos_score_reason"].startswith("tier B")
    assert props["revos_call_budget"] == "about fifty thousand"
    assert props["revos_call_budget_quote"].startswith("Our budget")
    assert props["revos_call_signer_quote"].startswith("Priya Nair, our Head")
    assert props["revos_lesson_ids"] == "L3,L1" and "revos_call_needs" not in props


# --- notes ----------------------------------------------------------------------------------------------------

def test_a_note_is_associated_with_the_deal_and_found_by_its_key_on_repeat(rig) -> None:
    crm, fake, _ = rig
    deal, _ = crm.upsert_deal("d1", {}, "kd")
    first = crm.add_note("note-key-1", deal, "Call summary: budget fifty thousand.")
    second = crm.add_note("note-key-1", deal, "Call summary: budget fifty thousand.")
    assert first == second and count(fake, "notes") == 1
    body = fake.objects["notes"][first]["properties"]["hs_note_body"]
    assert "Call summary" in body and note_token("note-key-1") in body and "revosdemo" in body
    assert ("notes", first, "?", deal) in fake.associations
    other = crm.add_note("note-key-2", deal, "Another note")
    assert other != first and count(fake, "notes") == 2


def test_a_note_whose_answer_was_lost_is_found_not_repeated(rig) -> None:
    crm, fake, _ = rig
    deal, _ = crm.upsert_deal("d1", {}, "kd")
    state = {"armed": True}

    def hook(kind):
        if kind == "notes" and state["armed"]:
            state["armed"] = False
            return httpx.Response(504, json={})

    fake.after_create = hook
    crm.add_note("nk", deal, "text")
    assert count(fake, "notes") == 1


# --- the CRM port and the outbox provider ------------------------------------------------------------------------

def test_find_by_key_looks_in_every_kind_and_in_notes(rig) -> None:
    crm, fake, _ = rig
    contact, _ = crm.upsert_contact("a@x.example", {}, "key-c")
    company, _ = crm.upsert_company("x.example", {}, "key-co")
    deal, _ = crm.upsert_deal("d1", {}, "key-d")
    note = crm.add_note("key-n", deal, "n")
    assert [crm.find_by_key(k) for k in ("key-c", "key-co", "key-d", "key-n", "missing")] == [
        contact, company, deal, note, None]
    assert crm.find("key-c") == contact


def test_the_port_upsert_needs_a_natural_key_and_knows_its_kinds(rig) -> None:
    crm, *_ = rig
    assert crm.upsert("contacts", "k", {"email": "z@x.example"})
    assert crm.upsert("companies", "k2", {"domain": "z.example"})
    assert crm.upsert("deals", "k3", {"revos_deal_id": "dd"})
    for kind, props in (("contacts", {}), ("companies", {}), ("deals", {})):
        with pytest.raises(PermanentError, match="needs"):
            crm.upsert(kind, "kk", props)
    with pytest.raises(PermanentError):
        crm.upsert("tickets", "k", {})


def test_get_record_and_list_records_with_paging(rig) -> None:
    crm, fake, _ = rig
    fake.page_size = 2
    ids = [crm.upsert_contact(f"u{i}@x.example", {}, f"k{i}")[0] for i in range(5)]
    assert crm.get_record("contacts", ids[0])["email"] == "u0@x.example"
    assert crm.get_record("contacts", "99999") is None
    assert sorted(r["id"] for r in crm.list_records("contacts")) == sorted(ids)


def action(type_, key, payload, **extra):
    return {"type": type_, "idempotency_key": key, "payload": payload, "to_email": None,
            "deal_id": None, **extra}


def test_perform_takes_identity_from_the_database_row_not_the_payload(rig) -> None:
    crm, fake, _ = rig
    crm.perform(action("crm_update", "k1", {"kind": "contacts", "properties": {"firstname": "Sam"},
                                            "email": "attacker@evil.test"},
                       to_email="sam@real.example"))
    emails = [r["properties"]["email"] for r in fake.objects["contacts"].values()]
    assert emails == ["sam@real.example"]


def test_perform_writes_a_deal_with_its_database_id_and_stage_and_associations(rig) -> None:
    crm, fake, _ = rig
    crm.perform(action("crm_update", "kc", {"kind": "contacts", "properties": {}}, to_email="s@x.example"))
    crm.perform(action("crm_update", "kco", {"kind": "companies", "domain": "x.example", "properties": {}}))
    deal_id = crm.perform(action("crm_update", "kd", {
        "kind": "deals", "stage": "qualified", "properties": {"dealname": "X"},
        "company_domain": "x.example"}, to_email="s@x.example", deal_id="11111111-aaaa"))
    props = fake.objects["deals"][deal_id]["properties"]
    assert props["revos_deal_id"] == "11111111-aaaa" and props["dealstage"] == "qualifiedtobuy"
    assert {a[2] for a in fake.associations if a[1] == deal_id} == {"contacts", "companies"}


def test_perform_adds_a_note_to_the_deal_looked_up_by_our_deal_id(rig) -> None:
    crm, fake, _ = rig
    deal, _ = crm.upsert_deal("our-deal-uuid", {}, "kd")
    note = crm.perform(action("crm_note", "kn", {"record_id": "our-deal-uuid", "text": "brief"}))
    assert ("notes", note, "?", deal) in fake.associations


def test_a_note_for_a_deal_not_in_hubspot_yet_is_a_permanent_error(rig) -> None:
    crm, *_ = rig
    with pytest.raises(PermanentError, match="not in HubSpot yet"):
        crm.perform(action("crm_note", "kn", {"record_id": "unknown", "text": "x"}))


def test_perform_refuses_what_it_does_not_own(rig) -> None:
    crm, *_ = rig
    with pytest.raises(PermanentError):
        crm.perform(action("first_touch", "k", {}))
    with pytest.raises(PermanentError):
        crm.perform(action("crm_update", "k", {"kind": "tickets", "properties": {}}))


def test_a_permission_error_is_reported_not_retried(rig) -> None:
    crm, fake, slept = rig
    fake.fail_next.append(403)
    with pytest.raises(HubSpotError) as err:
        crm.upsert_contact("a@x.example", {}, "k")
    assert err.value.category == "forbidden" and slept == []
