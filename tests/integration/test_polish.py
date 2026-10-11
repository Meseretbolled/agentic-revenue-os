"""Emails that read like a person wrote them: they acknowledge the reply, show times in the
prospect's time zone, and cite a labelled fact in the first email."""

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from revos.agents.common import evidence_from_fields
from revos.domain.drafting import Claim, Draft
from revos.ports.mail import InboundEmail
from revos.seed import load_profiles
from revos.skills.claims import check_draft
from revos.skills.slots import resolve_tz, slot_label
from tests.conftest import PACK_ID
from tests.pipeline_helpers import audit, make_rig

CHICAGO = ZoneInfo("America/Chicago")
SUNDAY = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)


def interested(body="Yes, interested, can we talk Thursday?", mid="<r1@x>"):
    return InboundEmail(mid, "sam.lee@brightside-optical.example", "me@x", "Re: hello", body,
                        datetime(2026, 10, 11, 9, 5, tzinfo=UTC))


def last_offer(conn):
    return conn.execute("SELECT payload FROM actions WHERE type = 'follow_up' ORDER BY created_at DESC "
                        "LIMIT 1").fetchone()[0]


def offer_to(conn, ro_conn, pack, clock, tz, start=SUNDAY, body="Yes, interested, can we talk Thursday?"):
    clock.set(start)
    rig = make_rig(conn, ro_conn, pack, clock)
    conn.execute("UPDATE contacts SET timezone = %s", (tz,))
    rig.orch.handle_reply(rig.deal_id, interested(body))
    return rig, last_offer(conn)


# --- 1. the wording of the reply email ------------------------------------------------------------------------

def test_the_meeting_email_acknowledges_the_reply_then_gives_the_times(conn, ro_conn, pack, clock):
    rig, offer = offer_to(conn, ro_conn, pack, clock, "UTC")
    body = offer["body"]
    assert body.startswith("Hi Sam,\n\nThanks for getting back to me, Sam. "
                           + pack.messaging.snippets["reply_ack_interested"])
    assert "I wanted to reach out" not in body and "local campaigns" not in body
    assert body.index("Thanks for getting back") < body.index(pack.messaging.snippets["slots_intro_local"]) \
        < body.index("- Thursday")
    assert offer["claims"] == [] and offer["template"] == "meeting_offer"


def test_a_reply_class_without_its_own_wording_gets_the_default_acknowledgement(conn, ro_conn, pack, clock):
    rig = make_rig(conn, ro_conn, pack, clock)
    slots = tuple(rig.world.calendar.free_slots(datetime(2026, 10, 13, 8, tzinfo=UTC),
                                                datetime(2026, 10, 14, 18, tzinfo=UTC), 30)[:3])
    run = rig.orch.run_agent(rig.deal_id, "meeting", "m.1", slots=slots, input_={"reply_label": "question"})
    assert run.status == "completed", run.human_reason
    body = run.proposals[0]["payload"]["body"]
    assert pack.messaging.snippets["reply_ack_default"] in body
    assert pack.messaging.snippets["reply_ack_interested"] not in body


def test_the_wording_is_in_the_pack(pack):
    assert "Thanks for getting back to me" in pack.messaging.templates["meeting_offer"].body
    assert {"reply_ack_interested", "first_touch_fallback", "follow_up_fallback"} <= set(pack.messaging.snippets)


def test_a_model_written_acknowledgement_with_an_undeclared_number_is_replaced_by_the_pack_wording(
        conn, ro_conn, pack, clock):
    from revos.adapters.fake_llm import FakeLLM
    from revos.ports.llm import DraftProposal
    bad = DraftProposal("It is up 73% since March.", ())
    rig = make_rig(conn, ro_conn, pack, clock, llm=FakeLLM(script=[bad, bad, bad]))
    rig.orch.handle_reply(rig.deal_id, interested())
    offer = last_offer(conn)
    assert "73%" not in offer["body"] and pack.messaging.snippets["reply_ack_interested"] in offer["body"]
    assert offer["draft_source"] == "template"
    (row,) = audit(conn, "draft.fell_back_to_template", rig.deal_id)
    print("CODES", row["checks_failed"])
    assert row["template"] == "meeting_offer" and row["checks_failed"]


# --- 2. time zones ----------------------------------------------------------------------------------------------

def test_a_slot_is_shown_in_the_prospects_time_zone_with_its_utc_offset() -> None:
    start = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)
    assert slot_label(start, CHICAGO) == "Thursday 15 October, 09:00 (your time, UTC-5)"
    assert slot_label(start, ZoneInfo("Asia/Kolkata")) == "Thursday 15 October, 19:30 (your time, UTC+5:30)"
    assert slot_label(start, ZoneInfo("UTC")) == "Thursday 15 October, 14:00 (your time, UTC+0)"
    assert slot_label(start, None) == "Thursday 15 October, 14:00 UTC"


def test_daylight_saving_changes_the_offset_for_the_same_utc_time() -> None:
    before = datetime(2026, 3, 6, 15, 0, tzinfo=UTC)          # US clocks go forward on 8 March 2026
    after = datetime(2026, 3, 9, 15, 0, tzinfo=UTC)
    assert slot_label(before, CHICAGO) == "Friday 06 March, 09:00 (your time, UTC-6)"
    assert slot_label(after, CHICAGO) == "Monday 09 March, 10:00 (your time, UTC-5)"
    assert slot_label(datetime(2026, 11, 2, 15, 0, tzinfo=UTC), CHICAGO).endswith("(your time, UTC-6)")  # back


def test_resolve_tz_never_guesses() -> None:
    assert resolve_tz("America/Chicago") == CHICAGO
    assert resolve_tz(None) is None and resolve_tz("") is None and resolve_tz("  ") is None
    assert resolve_tz("Mars/Olympus") is None and resolve_tz("CST") is None


def test_the_email_shows_local_times_inside_working_hours_and_keeps_utc_in_the_data(conn, ro_conn, pack, clock):
    rig, offer = offer_to(conn, ro_conn, pack, clock, "America/Chicago")
    assert offer["slots"] == ["2026-10-15T14:00:00+00:00", "2026-10-15T14:30:00+00:00",
                              "2026-10-15T15:00:00+00:00"]                              # UTC, always
    for text in ("- Thursday 15 October, 09:00 (your time, UTC-5)", "- Thursday 15 October, 09:30 (your "
                 "time, UTC-5)", "- Thursday 15 October, 10:00 (your time, UTC-5)"):
        assert text in offer["body"]
    assert pack.messaging.snippets["slots_intro_local"] in offer["body"] and "UTC)" not in offer["body"]
    (row,) = audit(conn, "slots.offered", rig.deal_id)
    assert row["slots_utc"] == offer["slots"] and row["timezone"] == "America/Chicago"
    assert row["shown_as"][0].startswith("- Thursday 15 October, 09:00")
    assert audit(conn, "slots.timezone_unknown") == []
    stored = conn.execute("SELECT fields->'offered_slots' FROM deals").fetchone()[0]
    assert stored == offer["slots"]


@pytest.mark.parametrize("start,offset", [(datetime(2026, 3, 1, 9, 0, tzinfo=UTC), "UTC-6"),
                                          (datetime(2026, 3, 8, 12, 0, tzinfo=UTC), "UTC-5")])
def test_an_offer_across_the_daylight_saving_change_uses_each_dates_own_offset(
        conn, ro_conn, pack, clock, start, offset):
    rig, offer = offer_to(conn, ro_conn, pack, clock, "America/Chicago", start)
    assert f"09:00 (your time, {offset})" in offer["body"]
    utc_hours = {datetime.fromisoformat(s).hour for s in offer["slots"]}
    assert utc_hours == ({15, 16} if offset == "UTC-6" else {14, 15})


@pytest.mark.parametrize("tz", [None, "Mars/Olympus"])
def test_an_unknown_time_zone_is_said_plainly_in_the_email_and_audited(conn, ro_conn, pack, clock, tz):
    rig, offer = offer_to(conn, ro_conn, pack, clock, tz)
    assert pack.messaging.snippets["slots_intro_utc"] in offer["body"]
    assert "- Thursday 15 October, 09:00 UTC" in offer["body"] and "(your time," not in offer["body"]
    assert offer["timezone"] is None
    assert len(audit(conn, "slots.timezone_unknown", rig.deal_id)) == 1


def test_a_reply_naming_a_local_time_books_the_matching_utc_slot_and_confirms_in_local_time(
        conn, ro_conn, pack, clock):
    rig, offer = offer_to(conn, ro_conn, pack, clock, "America/Chicago")
    rig.orch.handle_reply(rig.deal_id, interested("Thursday at 09:30 works for me", "<r2@x>"))
    (start,) = [r[0]["start"] for r in conn.execute("SELECT payload FROM actions WHERE type = 'book_meeting'")]
    assert start == "2026-10-15T14:30:00+00:00"
    confirmation = conn.execute("SELECT payload FROM actions WHERE type = 'follow_up' ORDER BY created_at "
                                "DESC LIMIT 1").fetchone()[0]
    assert "Thursday 15 October, 09:30 (your time, UTC-5)" in confirmation["body"]
    assert confirmation["invite"]["start"] == "2026-10-15T14:30:00+00:00"                # the invite is UTC


# --- 3. sourced facts in the first email -----------------------------------------------------------------------

def hot(conn, ro_conn, pack, clock, **kw):
    profile = load_profiles(PACK_ID)["hot"]
    fields = {**profile["fields"], "research_notes": profile["research_notes"]}
    rig = make_rig(conn, ro_conn, pack, clock, domain=profile["domain"], company=profile["company"],
                   fields=json.dumps(fields), **kw)
    first = rig.orch.handle_lead_created(rig.deal_id)
    return rig, first, conn.execute("SELECT payload FROM actions WHERE id = %s", (first.action_id,)).fetchone()[0]


def test_the_first_email_cites_a_labelled_fictional_fact_through_the_claim_checker(conn, ro_conn, pack, clock):
    rig, first, payload = hot(conn, ro_conn, pack, clock)
    assert first.status != "needs_human", first.reason
    body = payload["body"]
    assert ("Harborview Optical Collective runs 85 locations across Texas and Oklahoma. "
            "(Source: fictional demo profile (invented for this demo, not real research))") in body
    assert payload["claims"] and payload["claims"][0]["evidence"] == ["DEMO1"]
    assert "I noticed" not in body and "I read" not in body and "researched" not in body.lower()


def test_enrichment_stores_the_notes_as_sourced_facts_and_the_trace_says_they_are_fictional(
        conn, ro_conn, pack, clock):
    from revos.trace import timeline
    rig, _, _ = hot(conn, ro_conn, pack, clock)
    fields = conn.execute("SELECT fields FROM deals").fetchone()[0]
    assert fields["fact:DEMO1"].startswith("Harborview Optical Collective runs 85")
    assert fields["fact_source:DEMO1"] == "fictional demo profile (invented for this demo, not real research)"
    text = "\n".join(e.line() for e in timeline(conn, str(rig.deal_id)))
    assert "fictional demo data, NOT real research" in text
    assert "real research" not in text.replace("NOT real research", "").replace("not real research", "")


def test_real_research_takes_priority_over_demo_notes(conn, ro_conn, pack, clock):
    notes = [{"text": "Invented Co has 9 pretend sites.", "source": "fictional demo profile (x)"}]
    rig = make_rig(conn, ro_conn, pack, clock, fields=json.dumps({"research_notes": notes}))
    first = rig.orch.handle_lead_created(rig.deal_id)             # brightside: real pages in the mock web
    fields = conn.execute("SELECT fields FROM deals").fetchone()[0]
    assert "fact:F1" in fields and fields["fact_source:F1"].startswith("https://brightside")
    assert list(evidence_from_fields(fields).items)[0] == "F1"
    payload = conn.execute("SELECT payload FROM actions WHERE id = %s", (first.action_id,)).fetchone()[0]
    assert "I noticed that" in payload["body"] and "fictional" not in payload["body"]


def test_a_note_whose_source_does_not_say_fictional_is_labelled_anyway(conn, ro_conn, pack, clock):
    notes = [{"text": "Foo Ltd has 4 sites.", "source": "somewhere"}]
    profile = load_profiles(PACK_ID)["hot"]
    rig = make_rig(conn, ro_conn, pack, clock, domain=profile["domain"],
                   fields=json.dumps({**profile["fields"], "research_notes": notes}))
    rig.orch.handle_lead_created(rig.deal_id)
    assert conn.execute("SELECT fields->>'fact_source:DEMO1' FROM deals").fetchone()[0] == \
        "fictional demo profile: somewhere"


def test_removing_the_evidence_makes_the_claim_check_fail(conn, ro_conn, pack, clock):
    rig, first, payload = hot(conn, ro_conn, pack, clock)
    deal = rig.orch._deal(rig.deal_id)
    assert rig.orch._recheck_claims(deal, pack, payload) is True
    conn.execute("UPDATE deals SET fields = fields - 'fact:DEMO1' - 'fact_source:DEMO1'")
    assert rig.orch._recheck_claims(rig.orch._deal(rig.deal_id), pack, payload) is False


def test_the_checker_itself_rejects_a_claim_citing_evidence_that_is_not_there(pack) -> None:
    fields = {"fact:DEMO1": "Harborview Optical Collective runs 85 locations.",
              "fact_source:DEMO1": "fictional demo profile (x)"}
    line = "Harborview Optical Collective runs 85 locations. (Source: fictional demo profile (x))"
    draft = Draft("s", f"Hi Maya,\n\n{line}", (Claim(line, ("DEMO1",)),))
    approved = ["Hi Maya,"]

    def verified(evidence):
        return check_draft(draft, evidence, approved, ["Maya", "Harborview Optical Collective"], _Yes())

    assert verified(evidence_from_fields(fields)).passed
    assert not verified(evidence_from_fields({})).passed


class _Yes:
    def verify_claims(self, draft, evidence):
        from revos.ports.llm import ClaimVerdict
        return [ClaimVerdict(i, True, "ok") for i in range(len(draft.claims))]


def test_seed_profiles_carry_a_timezone_and_fictional_notes() -> None:
    for name, profile in load_profiles(PACK_ID).items():
        assert resolve_tz(profile["timezone"]) is not None, name
        assert profile["research_notes"] and all(
            n["source"].startswith("fictional demo profile") for n in profile["research_notes"])


def test_a_note_that_does_not_say_it_is_fictional_is_refused_by_the_loader(tmp_path) -> None:
    bad = tmp_path / "p.yaml"
    bad.write_text("x:\n  hot: {company: A, domain: a.example, contact: B, fields: {}, "
                   "research_notes: [{text: t, source: 'a magazine'}]}\n")
    with pytest.raises(ValueError, match="fictional demo profile"):
        load_profiles("x", bad)
