"""What HubSpot shows about a deal: its owner, the written score on the deal (properties and a note) and every
email on the contact's timeline. All idempotent, none of it able to break an email or a deal write."""

import random
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from revos.adapters.hubspot.client import HubSpotClient
from revos.adapters.hubspot.crm import EMAIL_ASSOCIATION, HubSpotCRM
from revos.cli.main import app
from revos.ports.mail import InboundEmail
from revos.runtime.worker import Worker
from tests.fakes.hubspot import TOKEN, FakeHubSpot
from tests.integration.test_crm_wiring import first_touch_sent
from tests.pipeline_helpers import CONTACT_EMAIL, make_rig

SELLER = {"id": "9001", "firstName": "Sara", "lastName": "Seller", "email": "sara@cool.example"}
OTHER = {"id": "9002", "firstName": "Omar", "lastName": "Other", "email": "omar@cool.example"}


def crm(fake: FakeHubSpot, **owner) -> HubSpotCRM:
    return HubSpotCRM(HubSpotClient(TOKEN, http=fake.client(), sleep=lambda s: None), **owner)


def drain(rig, conn, clock, rounds: int = 3):
    worker = Worker(rig.orch.handlers(), clock, rng=random.Random(1))
    for _ in range(rounds):
        clock.advance(3600)
        while worker.run_once(conn):
            pass


def rig_with(conn, ro_conn, pack, clock, fake, **owner):
    rig = make_rig(conn, ro_conn, pack, clock, crm=crm(fake, **owner), crm_sync=True)
    rig.orch.sender_email = "sender@cool.example"
    return rig


def reply(text="Yes, interested, can we talk Thursday?", mid="r1"):
    return InboundEmail(f"<{mid}@brightside-optical.example>", CONTACT_EMAIL, "sender@cool.example", "Re: hello",
                        text, datetime(2026, 10, 12, 9, 5, tzinfo=UTC))


def deal(fake):
    (record,) = fake.objects["deals"].values()
    return record


def contact(fake):
    (record,) = fake.objects["contacts"].values()
    return record


# --- the owner -----------------------------------------------------------------------------------------------------

def test_the_only_hubspot_user_becomes_the_owner_of_the_deal_and_the_contact(conn, ro_conn, pack, clock):
    fake = FakeHubSpot()
    fake.owner_list = [SELLER]
    rig = rig_with(conn, ro_conn, pack, clock, fake)
    first_touch_sent(rig)
    drain(rig, conn, clock)
    assert deal(fake)["properties"]["hubspot_owner_id"] == "9001"
    assert contact(fake)["properties"]["hubspot_owner_id"] == "9001"


def test_with_several_users_and_no_choice_nobody_is_assigned_and_the_reason_is_kept(conn, ro_conn, pack, clock):
    fake = FakeHubSpot()
    fake.owner_list = [SELLER, OTHER]
    rig = rig_with(conn, ro_conn, pack, clock, fake)
    first_touch_sent(rig)
    drain(rig, conn, clock)
    assert "hubspot_owner_id" not in deal(fake)["properties"]
    assert "2 HubSpot users exist" in rig.orch.provider.provider_for("crm_update").owner_note


@pytest.mark.parametrize("owner,expected", [({"owner_id": "9002"}, "9002"),
                                            ({"owner_email": "SARA@cool.example"}, "9001")])
def test_the_chosen_owner_is_used_by_id_or_by_email(conn, ro_conn, pack, clock, owner, expected):
    fake = FakeHubSpot()
    fake.owner_list = [SELLER, OTHER]
    rig = rig_with(conn, ro_conn, pack, clock, fake, **owner)
    first_touch_sent(rig)
    drain(rig, conn, clock)
    assert deal(fake)["properties"]["hubspot_owner_id"] == expected


def test_an_email_that_matches_no_user_assigns_nobody(conn, ro_conn, pack, clock):
    fake = FakeHubSpot()
    fake.owner_list = [SELLER, OTHER]
    rig = rig_with(conn, ro_conn, pack, clock, fake, owner_email="nobody@cool.example")
    first_touch_sent(rig)
    drain(rig, conn, clock)
    assert "hubspot_owner_id" not in deal(fake)["properties"]


def test_a_persons_reassignment_in_hubspot_is_never_undone(conn, ro_conn, pack, clock):
    fake = FakeHubSpot()
    fake.owner_list = [SELLER]
    rig = rig_with(conn, ro_conn, pack, clock, fake)
    first_touch_sent(rig)
    drain(rig, conn, clock)
    deal(fake)["properties"]["hubspot_owner_id"] = "7777"            # someone reassigned it by hand
    rig.orch.handle_reply(rig.deal_id, reply())
    drain(rig, conn, clock)
    assert deal(fake)["properties"]["hubspot_owner_id"] == "7777"


def test_a_service_key_without_the_owners_scope_still_writes_the_deal_and_says_why(conn, ro_conn, pack, clock):
    fake = FakeHubSpot()
    fake.owners_forbidden = True
    rig = rig_with(conn, ro_conn, pack, clock, fake)
    first_touch_sent(rig)
    drain(rig, conn, clock)
    assert deal(fake)["properties"]["revos_stage"] == "outreach"
    assert "hubspot_owner_id" not in deal(fake)["properties"]
    assert "403" in rig.orch.provider.provider_for("crm_update").owner_note
    assert conn.execute("SELECT count(*) FROM dead_letters").fetchone()[0] == 0


def test_the_owner_is_set_once_not_on_every_write(conn, ro_conn, pack, clock):
    fake = FakeHubSpot()
    fake.owner_list = [SELLER]
    rig = rig_with(conn, ro_conn, pack, clock, fake)
    first_touch_sent(rig)
    drain(rig, conn, clock)
    rig.orch.handle_reply(rig.deal_id, reply())
    drain(rig, conn, clock)
    owner_writes = [c for c in fake.calls if c[0] == "PATCH"]
    assert len(fake.objects["deals"]) == 1
    assert deal(fake)["properties"]["hubspot_owner_id"] == "9001"
    assert sum(1 for _ in owner_writes) < len(fake.calls)                      # sanity: not every call writes


# --- the written score -----------------------------------------------------------------------------------------------

def test_score_tier_and_every_rules_reason_are_properties_and_a_note_on_the_deal(conn, ro_conn, pack, clock):
    fake = FakeHubSpot()
    rig = rig_with(conn, ro_conn, pack, clock, fake)
    first_touch_sent(rig)
    drain(rig, conn, clock)
    total, tier, reason = conn.execute("SELECT total_score, tier, score_reason FROM deals").fetchone()
    props = deal(fake)["properties"]
    assert (int(props["revos_score"]), props["revos_tier"], props["revos_score_reason"]) == (total, tier, reason)
    rule_lines = [ln.strip() for ln in reason.splitlines() if ln.strip().startswith("[")]
    assert rule_lines and all(ln in props["revos_score_reason"] for ln in rule_lines)
    score_notes = [n["properties"]["hs_note_body"] for n in fake.objects["notes"].values()
                   if n["properties"]["hs_note_body"].startswith("Lead score")]
    assert len(score_notes) == 1 and f"Lead score {total}/100, tier {tier}" in score_notes[0]
    assert all(ln in score_notes[0] for ln in rule_lines)                        # each rule's written reason


def test_the_score_note_is_written_once_and_again_only_when_the_score_changes(conn, ro_conn, pack, clock):
    fake = FakeHubSpot()
    rig = rig_with(conn, ro_conn, pack, clock, fake)
    first_touch_sent(rig)
    drain(rig, conn, clock)

    def score_notes() -> int:
        return sum(1 for n in fake.objects["notes"].values()
                   if n["properties"]["hs_note_body"].startswith("Lead score"))
    rig.orch.sync_to_crm(rig.deal_id)
    rig.orch.sync_to_crm(rig.deal_id)
    drain(rig, conn, clock)
    assert score_notes() == 1
    conn.execute("UPDATE deals SET total_score = total_score + 1, score_reason = score_reason || ' (rescored)'")
    rig.orch.sync_to_crm(rig.deal_id)
    drain(rig, conn, clock)
    assert score_notes() == 2


# --- the emails ---------------------------------------------------------------------------------------------------------

def test_a_sent_email_is_logged_outbound_on_the_contact_and_the_deal(conn, ro_conn, pack, clock):
    fake = FakeHubSpot()
    rig = rig_with(conn, ro_conn, pack, clock, fake)
    first_touch_sent(rig)
    drain(rig, conn, clock)
    (email,) = fake.objects["emails"].values()
    props = email["properties"]
    subject, body = conn.execute("SELECT subject, body FROM messages WHERE direction = 'outbound'").fetchone()
    assert props["hs_email_direction"] == "EMAIL" and props["hs_email_subject"] == subject
    assert body.splitlines()[0] in props["hs_email_text"] and props["hs_timestamp"]
    assert f'"{CONTACT_EMAIL}"' in props["hs_email_headers"] and "sender@cool.example" in props["hs_email_headers"]
    linked = {(kind, to) for kind, rid, type_id, to in fake.association_types if rid == email["id"]
              for kind in [type_id]}
    assert linked == {(EMAIL_ASSOCIATION["contacts"], contact(fake)["id"]),
                      (EMAIL_ASSOCIATION["deals"], deal(fake)["id"])}


def test_a_received_reply_is_logged_inbound_with_the_senders_address(conn, ro_conn, pack, clock):
    fake = FakeHubSpot()
    rig = rig_with(conn, ro_conn, pack, clock, fake)
    first_touch_sent(rig)
    drain(rig, conn, clock)
    rig.orch.handle_reply(rig.deal_id, reply())
    drain(rig, conn, clock)
    incoming = [e["properties"] for e in fake.objects["emails"].values()
                if e["properties"]["hs_email_direction"] == "INCOMING_EMAIL"]
    assert len(incoming) == 1 and incoming[0]["hs_email_subject"] == "Re: hello"
    assert "can we talk Thursday" in incoming[0]["hs_email_text"]
    assert CONTACT_EMAIL in incoming[0]["hs_email_headers"]
    assert incoming[0]["hs_timestamp"].startswith("2026-10-12T09:05")


def test_the_same_email_is_never_logged_twice(conn, ro_conn, pack, clock):
    fake = FakeHubSpot()
    rig = rig_with(conn, ro_conn, pack, clock, fake)
    first_touch_sent(rig)
    drain(rig, conn, clock)
    rig.orch.handle_reply(rig.deal_id, reply())
    rig.orch.handle_reply(rig.deal_id, reply())                                 # delivered twice
    drain(rig, conn, clock)
    directions = sorted(e["properties"]["hs_email_direction"] for e in fake.objects["emails"].values())
    assert directions == ["EMAIL", "EMAIL", "INCOMING_EMAIL"]    # first touch, the meeting offer, the reply


def test_logging_an_email_again_with_the_same_key_finds_the_first(fake=None):
    fake = FakeHubSpot()
    adapter = crm(fake)
    args = dict(contact_id="1", deal_id="2", direction="outbound", subject="s", text="t",
                at="2026-10-12T09:00:00+00:00", from_addr="a@x.example", to_addr="b@y.example")
    first = adapter.log_email("deal:crm:email:<m1>", **args)
    assert adapter.log_email("deal:crm:email:<m1>", **args) == first
    assert adapter.find_by_key("deal:crm:email:<m1>") == first and len(fake.objects["emails"]) == 1
    assert adapter.find_by_key("deal:crm:email:<other>") is None


def test_a_lost_answer_after_the_email_was_logged_does_not_log_it_twice(conn, ro_conn, pack, clock):
    from revos.adapters.hubspot.client import HubSpotUnavailable
    fake = FakeHubSpot()
    adapter = crm(fake)
    calls = {"n": 0}

    def lose_the_answer(kind):
        calls["n"] += 1
        if kind == "emails" and calls["n"] == 1:
            raise HubSpotUnavailable("log email", "connection reset")
    fake.after_create = lose_the_answer
    adapter.log_email("d:crm:email:<m>", contact_id="1", deal_id="2", direction="inbound", subject="s",
                      text="t", at="2026-10-12T09:00:00+00:00", from_addr="b@y.example", to_addr="a@x.example")
    assert len(fake.objects["emails"]) == 1


def test_an_email_waits_for_its_contact_and_deal_instead_of_dead_lettering(conn, ro_conn, pack, clock):
    fake = FakeHubSpot()
    rig = rig_with(conn, ro_conn, pack, clock, fake)
    rig.orch.log_email_to_crm(rig.deal_id, "outbound", "<early@x.example>", "early", "body", clock.now(),
                              "a@x.example", CONTACT_EMAIL)
    drain(rig, conn, clock, rounds=1)
    assert fake.objects["emails"] == {} and conn.execute("SELECT count(*) FROM dead_letters").fetchone()[0] == 0
    rig.orch.sync_to_crm(rig.deal_id)
    drain(rig, conn, clock)
    assert len(fake.objects["emails"]) == 1


# --- the backfill ---------------------------------------------------------------------------------------------------------

def test_the_backfill_catches_up_a_deal_once_and_a_second_run_changes_nothing(conn, ro_conn, pack, clock):
    fake = FakeHubSpot()
    fake.owner_list = [SELLER]
    rig = make_rig(conn, ro_conn, pack, clock, crm=crm(fake), crm_sync=False)    # the deal predates CRM logging
    rig.orch.sender_email = "sender@cool.example"
    conn.execute("INSERT INTO messages (deal_id, contact_id, direction, message_id, subject, body) VALUES "
                 "(%s, %s, 'outbound', '<old-out@x.example>', 'First', 'hello'), "
                 "(%s, %s, 'inbound', '<old-in@x.example>', 'Re: First', 'yes please')",
                 (rig.deal_id, rig.contact_id, rig.deal_id, rig.contact_id))
    rig.orch.crm_sync = True
    assert rig.orch.backfill_crm(rig.deal_id) >= 4                       # deal, contact, score note, two emails
    drain(rig, conn, clock, rounds=6)
    snapshot = {k: len(v) for k, v in fake.objects.items()}
    assert snapshot["emails"] == 2 and deal(fake)["properties"]["hubspot_owner_id"] == "9001"
    assert rig.orch.backfill_crm(rig.deal_id) == 0
    drain(rig, conn, clock)
    assert {k: len(v) for k, v in fake.objects.items()} == snapshot


# --- the commands -------------------------------------------------------------------------------------------------------------

def test_hubspot_owners_lists_the_users_with_masked_addresses(monkeypatch):
    fake = FakeHubSpot()
    fake.owner_list = [SELLER, OTHER]
    monkeypatch.setattr("revos.cli.main._hubspot_client",
                        lambda: HubSpotClient(TOKEN, http=fake.client(), sleep=lambda s: None))
    result = CliRunner().invoke(app, ["hubspot-owners"])
    assert result.exit_code == 0 and "9001" in result.output and "Sara Seller" in result.output
    assert "s***@cool.example" in result.output and "sara@cool.example" not in result.output
    assert "2 owner(s)" in result.output


def test_hubspot_owners_says_which_scope_is_missing(monkeypatch):
    fake = FakeHubSpot()
    fake.owners_forbidden = True
    monkeypatch.setattr("revos.cli.main._hubspot_client",
                        lambda: HubSpotClient(TOKEN, http=fake.client(), sleep=lambda s: None))
    result = CliRunner().invoke(app, ["hubspot-owners"])
    assert result.exit_code == 1 and "crm.objects.owners.read" in result.output
    assert TOKEN not in result.output
