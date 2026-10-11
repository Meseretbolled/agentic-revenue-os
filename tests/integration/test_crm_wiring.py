"""HubSpot (a fake one) wired through the orchestrator: properties, notes, and failure isolation."""

import random

from revos.adapters.hubspot.client import HubSpotClient
from revos.adapters.hubspot.crm import HubSpotCRM
from revos.adapters.mock.crm import MockCRM
from revos.runtime.worker import Worker
from tests.fakes.hubspot import TOKEN, FakeHubSpot
from tests.integration.test_gmail_loop import first_touch_sent, gmail_rig
from tests.pipeline_helpers import audit, make_rig


def hubspot(fake: FakeHubSpot) -> HubSpotCRM:
    return HubSpotCRM(HubSpotClient(TOKEN, http=fake.client(), sleep=lambda s: None))


def drain(rig, conn, clock, step=0):
    worker = Worker(rig.orch.handlers(), clock, rng=random.Random(1))
    if step:
        clock.advance(step)
    while worker.run_once(conn):
        pass


def deal_props(fake):
    (deal,) = fake.objects["deals"].values()
    return deal["properties"]


def notes(fake):
    return [n["properties"]["hs_note_body"] for n in fake.objects["notes"].values()]


def test_the_deal_carries_stage_score_tier_reason_and_outreach_status(conn, ro_conn, pack, clock):
    fake = FakeHubSpot()
    rig = make_rig(conn, ro_conn, pack, clock, crm=hubspot(fake), crm_sync=True)
    first_touch_sent(rig)
    drain(rig, conn, clock)
    props = deal_props(fake)
    assert props["revos_deal_id"] == str(rig.deal_id) and props["revos_demo"] == "true"
    assert props["revos_stage"] == "outreach" and props["revos_outreach_status"]
    assert props["revos_score"] and props["revos_tier"] and props["revos_score_reason"]
    assert int(props["revos_sequence_day"]) == 1
    assert len(fake.objects["contacts"]) == 1 and len(fake.objects["companies"]) == 1


def test_every_email_sent_is_logged_on_the_contacts_timeline(conn, ro_conn, pack, clock):
    fake = FakeHubSpot()
    rig = make_rig(conn, ro_conn, pack, clock, crm=hubspot(fake), crm_sync=True)
    first_touch_sent(rig)
    drain(rig, conn, clock)
    drain(rig, conn, clock, step=3600)
    (subject,) = conn.execute("SELECT subject FROM messages WHERE direction = 'outbound'").fetchone()
    (email,) = fake.objects["emails"].values()
    assert email["properties"]["hs_email_subject"] == subject
    assert email["properties"]["hs_email_direction"] == "EMAIL"


def test_a_call_summary_is_a_note_with_the_call_fields(conn, ro_conn, pack, clock):
    fake = FakeHubSpot()
    rig = make_rig(conn, ro_conn, pack, clock, crm=hubspot(fake), crm_sync=True)
    first_touch_sent(rig)
    assert rig.orch.handle_transcript(rig.deal_id, "call-001").status == "completed"
    drain(rig, conn, clock)
    drain(rig, conn, clock, step=3600)
    assert len(notes(fake)) == 2 and any("call" in n.lower() for n in notes(fake)[1:])
    assert deal_props(fake).get("revos_lessons") is not None or deal_props(fake)["revos_stage"]


def test_resyncing_unchanged_data_queues_nothing_and_creates_no_duplicates(conn, ro_conn, pack, clock):
    fake = FakeHubSpot()
    rig = make_rig(conn, ro_conn, pack, clock, crm=hubspot(fake), crm_sync=True)
    first_touch_sent(rig)
    drain(rig, conn, clock)
    before = {k: len(v) for k, v in fake.objects.items()}
    assert rig.orch.sync_to_crm(rig.deal_id) == []
    drain(rig, conn, clock)
    assert {k: len(v) for k, v in fake.objects.items()} == before


def test_a_crm_outage_never_blocks_or_repeats_the_email(conn, ro_conn, pack, clock):
    server_rig, server = gmail_rig(conn, ro_conn, pack, clock, crm=None, crm_sync=True)
    fake = FakeHubSpot()
    server_rig.orch.provider._routes["crm_update"] = hubspot(fake)
    server_rig.orch.provider._routes["crm_note"] = hubspot(fake)
    fake.fail_next.extend([503] * 40)                       # HubSpot is down for the first calls
    first_touch_sent(server_rig)
    drain(server_rig, conn, clock)
    assert len(server.submitted) == 1                       # the email went out regardless
    assert conn.execute("SELECT status FROM actions WHERE type = 'first_touch'").fetchone()[0] == "sent"
    fake.fail_next.clear()
    for _ in range(6):
        drain(server_rig, conn, clock, step=3600)
    assert len(server.submitted) == 1                       # recovering the CRM never re-sends it
    assert deal_props(fake)["revos_stage"] == "outreach" and len(notes(fake)) == 1


def test_a_retried_email_never_duplicates_the_crm_record_or_note(conn, ro_conn, pack, clock):
    rig, server = gmail_rig(conn, ro_conn, pack, clock, crm_sync=True)
    fake = FakeHubSpot()
    rig.orch.provider._routes["crm_update"] = hubspot(fake)
    rig.orch.provider._routes["crm_note"] = hubspot(fake)
    first = rig.orch.handle_lead_created(rig.deal_id)
    server.crash_after_submit = True
    try:
        rig.orch.send_action(first.action_id)
    except Exception:
        pass
    assert rig.orch.send_action(first.action_id).status == "sent"
    drain(rig, conn, clock)
    drain(rig, conn, clock, step=600)
    assert len(server.submitted) == 1
    assert {k: len(v) for k, v in fake.objects.items()} == {"contacts": 1, "companies": 1, "deals": 1,
                                                            "notes": 1, "emails": 1}      # one score note, one email


def test_a_note_waits_for_its_deal_instead_of_dead_lettering(conn, ro_conn, pack, clock):
    fake = FakeHubSpot()
    rig = make_rig(conn, ro_conn, pack, clock, crm=hubspot(fake), crm_sync=True)
    rig.orch._crm_note(rig.deal_id, f"{rig.deal_id}:early", "an early note")
    drain(rig, conn, clock)
    assert notes(fake) == [] and conn.execute("SELECT count(*) FROM dead_letters").fetchone()[0] == 0
    rig.orch.sync_to_crm(rig.deal_id)
    drain(rig, conn, clock, step=3600)
    drain(rig, conn, clock, step=3600)
    assert [n.split("\n")[0] for n in notes(fake)] == ["an early note"]


def test_the_mock_crm_receives_the_same_writes(conn, ro_conn, pack, clock):
    crm = MockCRM(seed=False)
    rig = make_rig(conn, ro_conn, pack, clock, crm=crm, crm_sync=True)
    first_touch_sent(rig)
    drain(rig, conn, clock)
    drain(rig, conn, clock, step=3600)
    (deal,) = crm.list_records("deals")
    assert deal["revos_stage"] == "outreach" or deal.get("stage") == "outreach"
    assert len(crm.list_records("notes")) == 1
    n = len(crm.calls)
    rig.orch.sync_to_crm(rig.deal_id)
    drain(rig, conn, clock)
    assert len(crm.calls) == n


def test_an_old_snapshot_that_waited_out_an_outage_cannot_overwrite_a_newer_one(conn, ro_conn, pack, clock):
    fake = FakeHubSpot()
    rig = make_rig(conn, ro_conn, pack, clock, crm=hubspot(fake), crm_sync=True)
    rig.orch.sync_to_crm(rig.deal_id)                      # a snapshot at the first stage
    conn.execute("UPDATE deals SET total_score = 77, tier = 'A', score_reason = 'r' WHERE id = %s",
                 (rig.deal_id,))
    (newer,) = rig.orch.sync_to_crm(rig.deal_id)           # a newer one, queued later
    drain(rig, conn, clock)
    assert deal_props(fake)["revos_score"] in (77, "77")
    assert audit(conn, "crm.write_superseded", rig.deal_id)
    assert conn.execute("SELECT status FROM actions WHERE id = %s", (newer,)).fetchone()[0] == "sent"
