import pytest

from revos.adapters.hubspot.client import HubSpotClient, HubSpotError
from revos.adapters.hubspot.crm import HubSpotCRM
from revos.adapters.hubspot.reset import find_demo_records, reset_demo
from revos.adapters.hubspot.schema import CALL_FIELDS, PROPERTIES, ensure_properties
from tests.fakes.hubspot import TOKEN, FakeHubSpot


@pytest.fixture
def rig():
    fake = FakeHubSpot()
    client = HubSpotClient(TOKEN, http=fake.client(), sleep=lambda s: None)
    return client, fake


# --- hubspot-setup ------------------------------------------------------------------------------------------

def test_setup_creates_every_property_we_need(rig) -> None:
    client, fake = rig
    report = ensure_properties(client)
    deals = set(fake.properties["deals"])
    for name in ("revos_deal_id", "revos_stage", "revos_score", "revos_tier", "revos_score_reason",
                 "revos_outreach_status", "revos_sequence_day", "revos_lesson_ids", "revos_demo",
                 "revos_key"):
        assert name in deals, name
    for field in CALL_FIELDS:                              # every call field, and its quote
        assert f"revos_call_{field}" in deals and f"revos_call_{field}_quote" in deals
    assert {"revos_demo", "revos_key"} <= set(fake.properties["contacts"])
    assert {"revos_demo", "revos_key"} <= set(fake.properties["companies"])
    assert len(report.created) == sum(len(v) for v in PROPERTIES.values())
    assert set(report.groups_created) == {"contacts", "companies", "deals"}


def test_setup_property_shapes(rig) -> None:
    client, fake = rig
    ensure_properties(client)
    deals = fake.properties["deals"]
    assert deals["revos_demo"]["type"] == "enumeration" and deals["revos_demo"]["fieldType"] == "booleancheckbox"
    assert {o["value"] for o in deals["revos_demo"]["options"]} == {"true", "false"}
    assert deals["revos_score"]["type"] == "number" and deals["revos_sequence_day"]["type"] == "number"
    assert deals["revos_deal_id"]["hasUniqueValue"] is True
    assert deals["revos_score_reason"]["fieldType"] == "textarea"
    assert all(p["groupName"] == "revos" for p in deals.values())


def test_running_setup_twice_changes_nothing(rig) -> None:
    client, fake = rig
    ensure_properties(client)
    snapshot = {k: dict(v) for k, v in fake.properties.items()}
    writes_before = sum(1 for m, _ in fake.calls if m == "POST")
    second = ensure_properties(client)
    assert second.created == [] and second.groups_created == [] and not second.changed
    assert len(second.existing) == sum(len(v) for v in PROPERTIES.values())
    assert fake.properties == snapshot
    assert sum(1 for m, _ in fake.calls if m == "POST") == writes_before         # not a single write


def test_setup_only_creates_what_is_missing(rig) -> None:
    client, fake = rig
    ensure_properties(client)
    del fake.properties["deals"]["revos_score"]
    report = ensure_properties(client)
    assert report.created == ["deals.revos_score"] and "revos_score" in fake.properties["deals"]


def test_a_lost_race_on_a_property_is_not_an_error(rig) -> None:
    client, fake = rig
    original = fake.route

    def racing(method, path, params, body):
        if method == "POST" and path == "/crm/v3/properties/deals" and body["name"] == "revos_tier":
            fake.properties["deals"]["revos_tier"] = body       # someone else made it just now
            return __import__("httpx").Response(409, json={})
        return original(method, path, params, body)

    fake.route = racing
    report = ensure_properties(client)
    assert "deals.revos_tier" in report.existing and "deals.revos_tier" not in report.created


def test_missing_scopes_fail_with_a_category_not_a_crash(rig) -> None:
    client, fake = rig
    fake.fail_next.append(403)
    with pytest.raises(HubSpotError) as err:
        ensure_properties(client)
    assert err.value.category == "forbidden"


def test_stages_our_map_needs_but_the_pipeline_lacks_are_reported(rig) -> None:
    client, fake = rig
    fake.pipeline_stages.remove("contractsent")
    assert ensure_properties(client).stages_missing == ["contractsent"]
    fake.pipeline_stages.append("contractsent")
    assert ensure_properties(client).stages_missing == []


# --- reset-demo --hubspot --------------------------------------------------------------------------------------

def populate(client, fake):
    crm = HubSpotCRM(client)
    contact, _ = crm.upsert_contact("demo@x.example", {}, "k1")
    company, _ = crm.upsert_company("demo.example", {}, "k2")
    deal, _ = crm.upsert_deal("d1", {}, "k3", contact_id=contact, company_id=company)
    note = crm.add_note("k4", deal, "a note")
    # records somebody else owns: no flag, or flagged false
    for kind in ("contacts", "companies", "deals"):
        fake.objects[kind][f"keep-{kind}"] = {"id": f"keep-{kind}", "properties": {"name": "real customer"}}
        fake.objects[kind][f"false-{kind}"] = {"id": f"false-{kind}", "properties": {"revos_demo": "false"}}
    fake.objects["notes"]["keep-note"] = {"id": "keep-note", "properties": {"hs_note_body": "a real note"}}
    return {"contacts": contact, "companies": company, "deals": deal, "notes": note}


def test_reset_deletes_only_flagged_records(rig) -> None:
    client, fake = rig
    ours = populate(client, fake)
    counts = reset_demo(client)
    assert counts == {"deals": 1, "contacts": 1, "companies": 1, "notes": 1}
    for kind, record_id in ours.items():
        assert record_id not in fake.objects[kind]
    for kind in ("contacts", "companies", "deals"):
        assert set(fake.objects[kind]) == {f"keep-{kind}", f"false-{kind}"}        # untouched
    assert set(fake.objects["notes"]) == {"keep-note"}


def test_a_dry_run_counts_and_deletes_nothing(rig) -> None:
    client, fake = rig
    populate(client, fake)
    before = {k: set(v) for k, v in fake.objects.items()}
    assert reset_demo(client, dry_run=True) == {"deals": 1, "contacts": 1, "companies": 1, "notes": 1}
    assert {k: set(v) for k, v in fake.objects.items()} == before
    assert not any(m == "DELETE" for m, _ in fake.calls)


def test_a_record_the_search_returns_without_the_flag_is_never_deleted(rig) -> None:
    client, fake = rig
    populate(client, fake)
    real = client.search_all

    def sloppy(kind, filters, properties, **kw):
        yield {"id": f"keep-{kind}", "properties": {"revos_demo": "false"}}      # a wrong hit
        yield from real(kind, filters, properties, **kw)

    client.search_all = sloppy
    found = find_demo_records(client)
    assert all(f"keep-{k}" not in found[k] for k in ("contacts", "companies", "deals"))
    reset_demo(client)
    assert all(f"keep-{k}" in fake.objects[k] for k in ("contacts", "companies", "deals"))


def test_note_ids_recorded_by_the_outbox_are_deleted_too_and_a_missing_one_is_fine(rig) -> None:
    client, fake = rig
    fake.objects["notes"]["nours"] = {"id": "nours", "properties": {"hs_note_body": "x"}}
    counts = reset_demo(client, extra_note_ids=["nours", "nalreadygone"])
    assert counts["notes"] == 2 and "nours" not in fake.objects["notes"]


def test_reset_handles_more_than_one_page(rig) -> None:
    client, fake = rig
    fake.page_size = 2
    crm = HubSpotCRM(client)
    for i in range(5):
        crm.upsert_contact(f"p{i}@x.example", {}, f"k{i}")
    assert reset_demo(client)["contacts"] == 5 and fake.objects["contacts"] == {}
