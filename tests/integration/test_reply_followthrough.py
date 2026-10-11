"""What the system does after a reply: the evidence quote, a note in the CRM, and slots on the day
the prospect asked for."""

from datetime import UTC, datetime

from revos.ports.mail import InboundEmail
from tests.fakes.hubspot import FakeHubSpot
from tests.integration.test_crm_wiring import drain, hubspot, notes
from tests.pipeline_helpers import audit, make_rig

SUNDAY = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
BODY = ("Yes, interested, can we talk Thursday?\n\nOn Sun, Oct 11, 2026 at 11:51 AM Jo <jo@x.org>\nwrote:\n\n"
        "> Hi Maya, would a call be useful?\n")


def mail(body=BODY, mid="<r1@x>"):
    return InboundEmail(mid, "sam.lee@brightside-optical.example", "me@x", "Re: hello", body,
                        datetime(2026, 10, 11, 9, 5, tzinfo=UTC))


def sent_slots(conn):
    row = conn.execute("SELECT payload->'slots' FROM actions WHERE type = 'follow_up'").fetchone()
    return [datetime.fromisoformat(s) for s in row[0]]


def test_the_classification_carries_the_evidence_quote_and_the_trace_shows_it(conn, ro_conn, pack, clock):
    from revos.trace import timeline
    clock.set(SUNDAY)
    rig = make_rig(conn, ro_conn, pack, clock)
    rig.orch.handle_reply(rig.deal_id, mail())
    (payload,) = [r[0] for r in conn.execute("SELECT payload FROM actions WHERE type = 'classify_reply'")]
    assert payload["label"] == "interested" and payload["evidence_quote"] == \
        "Yes, interested, can we talk Thursday?"
    assert any('"Yes, interested, can we talk Thursday?"' in e.why or '"Yes, interested' in e.what
               for e in timeline(conn, str(rig.deal_id)) if e.kind == "proposal")


def test_meeting_slots_are_offered_on_the_weekday_the_reply_asked_for(conn, ro_conn, pack, clock):
    clock.set(SUNDAY)
    rig = make_rig(conn, ro_conn, pack, clock)
    rig.orch.handle_reply(rig.deal_id, mail())
    slots = sent_slots(conn)
    assert len(slots) == 3 and {s.weekday() for s in slots} == {3}              # all Thursday
    assert audit(conn, "slots.preference_unmet") == []


def test_without_a_weekday_the_next_free_slots_are_offered(conn, ro_conn, pack, clock):
    clock.set(SUNDAY)
    rig = make_rig(conn, ro_conn, pack, clock)
    rig.orch.handle_reply(rig.deal_id, mail("Yes, interested, happy to chat."))
    assert {s.weekday() for s in sent_slots(conn)} == {0}                         # Monday, the next day


def test_a_weekday_with_no_free_slot_falls_back_and_says_so(conn, ro_conn, pack, clock):
    clock.set(SUNDAY)
    rig = make_rig(conn, ro_conn, pack, clock)
    rig.orch.handle_reply(rig.deal_id, mail("Yes, interested, can we talk Saturday?"))
    assert {s.weekday() for s in sent_slots(conn)} == {0}
    assert audit(conn, "slots.preference_unmet")


def test_a_weekday_only_in_the_quoted_history_is_ignored(conn, ro_conn, pack, clock):
    clock.set(SUNDAY)
    rig = make_rig(conn, ro_conn, pack, clock)
    rig.orch.handle_reply(rig.deal_id, mail("Yes, interested!\n\nOn Sun Jo wrote:\n> how about Thursday?"))
    assert {s.weekday() for s in sent_slots(conn)} == {0}


def test_a_reply_leaves_a_note_in_hubspot_with_the_evidence_and_only_the_new_text(conn, ro_conn, pack, clock):
    clock.set(SUNDAY)
    fake = FakeHubSpot()
    rig = make_rig(conn, ro_conn, pack, clock, crm=hubspot(fake), crm_sync=True)
    rig.orch.handle_reply(rig.deal_id, mail())
    drain(rig, conn, clock)
    drain(rig, conn, clock, step=3600)
    (note,) = [n for n in notes(fake) if n.startswith("Reply received")]
    assert 'Classified as: interested (evidence: "Yes, interested, can we talk Thursday?")' in note
    assert "would a call be useful" not in note                                  # the quoted history
    rig.orch.handle_reply(rig.deal_id, mail())                                    # delivered twice: no new note
    drain(rig, conn, clock)
    assert len([n for n in notes(fake) if n.startswith("Reply received")]) == 1
