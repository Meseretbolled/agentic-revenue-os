from datetime import UTC, datetime

import pytest

from revos.adapters.mock import MockESign
from revos.domain.errors import WebhookRejected
from revos.domain.untrusted import UntrustedText
from revos.ports.mail import build_ics
from revos.skills.handoff import build_handoff
from revos.skills.slots import match_slot, slot_text

OFFERED = [datetime(2026, 10, 13, 11, 0, tzinfo=UTC), datetime(2026, 10, 13, 11, 30, tzinfo=UTC),
           datetime(2026, 10, 14, 10, 0, tzinfo=UTC)]


def reply(text: str) -> UntrustedText:
    return UntrustedText.wrap("reply", "m", text)


@pytest.mark.parametrize("text,expected", [
    ("Tuesday 13 October at 11:00 works for me", 0),
    ("Tuesday at 11:30 works", 1),
    ("the second one works for me", 1),
    ("option 3 please", 2),
    ("Wednesday works for me", 2),
    ("14 October at 10:00", 2),
    ("10:00 please", 2),
])
def test_an_acceptance_that_names_one_offered_time_matches_it(text, expected) -> None:
    assert match_slot(reply(text), OFFERED) == OFFERED[expected]


@pytest.mark.parametrize("text", [
    "that works for me",                             # names nothing
    "Tuesday works for me",                          # two Tuesday slots
    "Tuesday 13 October at 11:00 or 11:30",          # two named
    "Friday at 15:00 works",                         # not offered
    "Tuesday 13 October at 15:00",                   # right day, wrong time
    "the fourth one",                                # no fourth
    "",
])
def test_an_acceptance_that_does_not_single_out_an_offered_time_matches_nothing(text) -> None:
    assert match_slot(reply(text), OFFERED) is None


def test_slots_are_written_the_same_way_in_every_email() -> None:
    assert slot_text(OFFERED[0]) == "Tuesday 13 October 11:00 UTC"


def test_the_invite_names_only_the_address_it_is_given() -> None:
    ics = build_ics("uid-1", OFFERED[0], 30, "Intro call", "sam@brightside-optical.example")
    assert ics.filename == "invite.ics" and "method=REQUEST" in ics.content_type
    lines = ics.content.splitlines()
    assert "ATTENDEE;RSVP=TRUE:mailto:sam@brightside-optical.example" in lines
    assert "DTSTART:20261013T110000Z" in lines and "DTEND:20261013T113000Z" in lines
    assert "UID:uid-1@revos.example" in lines and lines[0] == "BEGIN:VCALENDAR"
    assert sum(1 for line in lines if line.startswith("ATTENDEE")) == 1


def test_a_signature_webhook_must_verify() -> None:
    esign = MockESign(secret="s3cret")
    body = esign.signed_event("env-9")
    event = esign.parse_event(body)
    assert (event.kind, event.envelope_id) == ("all_signed", "env-9")
    assert event.event_id == esign.parse_event(esign.signed_event("env-9")).event_id    # stable
    tampered = {**body, "event": {**body["event"], "event_hash": "0" * 64}}
    with pytest.raises(WebhookRejected, match="does not verify"):
        esign.parse_event(tampered)
    other_key = MockESign(secret="another")
    with pytest.raises(WebhookRejected):
        other_key.parse_event(body)                                  # signed with a different key
    retyped = {**body, "event": {**body["event"], "event_type": "signature_request_declined"}}
    with pytest.raises(WebhookRejected):
        esign.parse_event(retyped)                                   # the type is covered by the hash


@pytest.mark.parametrize("payload", [{}, {"event": {}}, {"event": "x"}, None,
                                     {"event": {"event_type": "a", "event_time": "1", "event_hash": "h"}}])
def test_a_malformed_webhook_is_rejected_not_crashed_on(payload) -> None:
    with pytest.raises(WebhookRejected):
        MockESign().parse_event(payload)


def test_different_event_times_are_different_events() -> None:
    esign = MockESign()
    first = esign.parse_event(esign.signed_event("env-1", event_time="1760000000"))
    second = esign.parse_event(esign.signed_event("env-1", event_time="1760000999"))
    assert first.event_id != second.event_id


def test_the_handoff_has_summary_signer_terms_and_open_items() -> None:
    text = build_handoff(
        company="Acme", contact="Sam Lee", tier="B", total=61,
        fields={"decision_maker_name": "Dana Park", "decision_maker_role": "CMO", "signer_name": "Priya Nair",
                "signer_role": "Head of Finance", "io_start_date": "2026-11-02", "io_end_date": "2027-02-01",
                "discount_pct": 10, "objections": "attribution worries us"},
        terms={"total": 11070.0, "line_items": [{"quantity": 3, "name": "Creative", "unit_price": 2500,
                                                  "unit": "per month"}]},
        rate_card_label="SAMPLE rate card", envelope_id="env-1", lesson_ids=[4, 2])
    for expected in ("Closed won: Acme", "tier B (61/100)", "Decision maker Dana Park (CMO)",
                     "Signer: Priya Nair (Head of Finance)", "3 x Creative at 2500 per month",
                     "Total 11070.0 after a 10% discount", "2026-11-02 to 2027-02-01", "SAMPLE rate card",
                     "TEST MODE", "not legally binding", "raised on the call: attribution worries us",
                     "agree the kick-off", "replace the sample terms", "L4, L2"):
        assert expected in text, expected
