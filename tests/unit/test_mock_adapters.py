import re
from datetime import UTC, datetime

import pytest

from revos.adapters.mock import build_mock_world
from revos.adapters.mock.fixtures import FIXTURES, load
from revos.adapters.router import ActionRouter
from revos.ports.esign import SignatureRequest
from revos.ports.mail import InboundEmail, OutboundEmail, message_id_for

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


@pytest.fixture
def world():
    return build_mock_world()


# --- fixtures are fictional ----------------------------------------------------------------------

def test_every_address_and_url_in_the_fixtures_is_on_a_reserved_domain() -> None:
    pattern = re.compile(r"[\w.+-]+@([\w.-]+)|https?://([\w.-]+)")
    for path in FIXTURES.glob("*.yaml"):
        for match in pattern.finditer(path.read_text()):
            host = (match.group(1) or match.group(2)).lower().rstrip(".")
            assert host.endswith((".example", ".test", ".invalid")) or host == "example.com", \
                f"{path.name}: {host} is not a reserved domain"


# --- mailer and inbox ------------------------------------------------------------------------------

def test_mailer_records_every_send_even_a_duplicate(world) -> None:
    mail = OutboundEmail("k1", "a@x.example", "Hi", "Body")
    first, second = world.mailer.send(mail), world.mailer.send(mail)
    assert first == second == message_id_for("k1")
    assert len(world.mailer.sent) == 2                      # a double send is visible to tests
    assert world.mailer.find_sent("k1") == first and world.mailer.find_sent("nope") is None


def test_message_ids_are_deterministic_and_distinct() -> None:
    assert message_id_for("a") == message_id_for("a") != message_id_for("b")
    assert message_id_for("a").startswith("<") and message_id_for("a").endswith("@revos.example>")


def test_mailer_as_a_provider_uses_the_database_recipient_not_the_payload(world) -> None:
    action = {"type": "first_touch", "idempotency_key": "k", "to_email": "real@x.example",
              "payload": {"subject": "S", "body": "B", "to": "attacker@evil.test"}}
    world.mailer.perform(action)
    assert [m.to for m in world.mailer.sent] == ["real@x.example"]


def test_mailer_refuses_to_send_without_a_database_recipient(world) -> None:
    action = {"type": "first_touch", "idempotency_key": "k", "to_email": None,
              "payload": {"subject": "S", "body": "B", "to": "attacker@evil.test"}}
    with pytest.raises(ValueError, match="no recipient"):
        world.mailer.perform(action)
    assert world.mailer.sent == []


def test_mailer_refuses_action_types_it_does_not_own(world) -> None:
    with pytest.raises(ValueError):
        world.mailer.perform({"type": "send_io", "idempotency_key": "k", "to_email": "a@x.example",
                              "payload": {}})


def test_mailer_can_simulate_a_crash_after_the_server_accepted(world) -> None:
    world.mailer.crash_after_send = True
    with pytest.raises(ConnectionError):
        world.mailer.send(OutboundEmail("k", "a@x.example", "S", "B"))
    assert world.mailer.find_sent("k") is not None          # the provider-side lookup still finds it


def test_inbox_delivers_once_until_marked_processed(world) -> None:
    mail = InboundEmail("<m1>", "a@x.example", "me@x.example", "Re", "hi", NOW)
    world.inbox.deliver(mail)
    world.inbox.deliver(mail)                               # same Message-ID twice
    assert world.inbox.poll() == [mail] and world.inbox.poll() == [mail]
    world.inbox.mark_processed("<m1>")
    assert world.inbox.poll() == [] and world.inbox.polls == 3


def test_inbox_serves_the_fixture_replies_including_the_injection_demo(world) -> None:
    for fragment in ("price", "injection", "ooo", "stop"):
        world.inbox.deliver_fixture(fragment, "me@sender.example", NOW)
    bodies = [m.body for m in world.inbox.poll()]
    assert len(bodies) == 4 and any("IGNORE PREVIOUS INSTRUCTIONS" in b for b in bodies)
    with pytest.raises(KeyError):
        world.inbox.deliver_fixture("missing", "me@sender.example")


# --- CRM -----------------------------------------------------------------------------------------------

def test_crm_upsert_is_idempotent_by_key_and_updates_in_place(world) -> None:
    a = world.crm.upsert("contacts", "k1", {"email": "a@x.example"})
    b = world.crm.upsert("contacts", "k1", {"firstname": "Ann"})
    assert a == b
    assert world.crm.get_record("contacts", a) == {"id": a, "email": "a@x.example", "firstname": "Ann"}
    assert world.crm.find_by_key("k1") == a


def test_crm_is_seeded_with_fictional_records_and_notes_are_idempotent(world) -> None:
    assert world.crm.list_records("companies")[0]["name"] == "Brightside Optical Group"
    record = world.crm.list_records("deals")[0]["id"]
    n1, n2 = (world.crm.add_note("note:1", record, "called"), world.crm.add_note("note:1", record, "x"))
    assert n1 == n2 and len(world.crm.list_records("notes")) == 1
    assert world.crm.get_record("deals", "missing") is None


def test_crm_as_a_provider(world) -> None:
    pid = world.crm.perform({"type": "crm_update", "idempotency_key": "u1",
                             "payload": {"kind": "deals", "properties": {"tier": "A"}}})
    assert world.crm.get_record("deals", pid)["tier"] == "A" and world.crm.find("u1") == pid
    with pytest.raises(ValueError):
        world.crm.perform({"type": "first_touch", "idempotency_key": "x", "payload": {}})


# --- chat, transcripts, research, calendar, e-sign ---------------------------------------------------------

def test_chat_records_posts_and_finds_them_by_key(world) -> None:
    ts = world.chat.post("#revops", "hello", "p1")
    assert world.chat.find_by_key("p1") == ts and world.chat.posts[0].channel == "#revops"
    assert world.chat.find_by_key("zzz") is None


def test_transcripts_serve_the_fictional_call(world) -> None:
    assert world.transcripts.list_ready() == ["call-001", "call-002"]
    t = world.transcripts.get("call-001")
    assert "budget is about fifty thousand" in t.text and world.transcripts.get("nope") is None
    assert world.transcripts.fetched == ["call-001", "nope"]
    # shaped like Fireflies: title, date, participants, sentences with speaker_name and text
    assert t.title.startswith("Discovery call") and t.date.year == 2026
    assert t.participants == ("Jo Rivers", "Sam Lee")
    first = t.sentences[0]
    assert (first.index, first.speaker_name) == (0, "Jo Rivers") and first.text.startswith("Thanks")
    assert t.text.splitlines()[1] == "Sam Lee: We need local campaigns for all our practices " \
        "without every owner redoing the creative."


def test_research_search_and_fetch(world) -> None:
    hits = world.research.search("Brightside optical")
    assert hits and all(h.url.startswith("https://brightside-optical.example") for h in hits)
    assert world.research.search("unknown company") == []
    assert world.research.fetch("https://nowhere.example/") is None
    assert "120 locations" in world.research.fetch("https://brightside-optical.example/").content


def test_calendar_offers_working_hour_slots_that_avoid_busy_time(world) -> None:
    start, end = datetime(2026, 10, 13, 8, 0, tzinfo=UTC), datetime(2026, 10, 13, 18, 0, tzinfo=UTC)
    slots = world.calendar.free_slots(start, end, 30)
    hours = {(s.start.hour, s.start.minute) for s in slots}
    assert (9, 0) not in hours and (10, 30) not in hours and (13, 0) not in hours   # busy
    assert (11, 0) in hours and (14, 0) in hours and (8, 0) not in hours            # before 9: closed
    assert all(s.end.hour * 60 + s.end.minute <= 17 * 60 for s in slots)
    saturday = world.calendar.free_slots(datetime(2026, 10, 17, 8, tzinfo=UTC),
                                         datetime(2026, 10, 17, 18, tzinfo=UTC), 30)
    assert saturday == []


def test_calendar_invites_are_idempotent_and_block_the_slot(world) -> None:
    start = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
    a = world.calendar.create_invite("m1", "Intro", start, ("a@x.example",))
    b = world.calendar.create_invite("m1", "Intro", start, ("a@x.example",))
    assert a == b and len(world.calendar.invites) == 1 and world.calendar.find_invite("m1") == a
    free = world.calendar.free_slots(start.replace(hour=8), start.replace(hour=12), 30)
    assert all(s.start != start for s in free)


def test_esign_is_test_mode_only_and_can_simulate_a_signature(world) -> None:
    req = SignatureRequest("k", "IO", "doc", "a@x.example", "Ann")
    envelope = world.esign.send(req)
    assert world.esign.send(req) == envelope and world.esign.find_by_key("k") == envelope
    assert world.esign.status(envelope) == "awaiting"
    world.esign.simulate_signed(envelope)
    assert world.esign.status(envelope) == "signed"
    with pytest.raises(ValueError, match="test mode"):
        world.esign.send(SignatureRequest("k2", "IO", "doc", "a@x.example", "Ann", test_mode=False))


def test_esign_provider_takes_the_signer_from_the_database_row(world) -> None:
    pid = world.esign.perform({"type": "send_io", "idempotency_key": "s1", "to_email": "db@x.example",
                               "payload": {"title": "IO", "document": "d", "signer_name": "Ann",
                                           "signer_email": "attacker@evil.test"}})
    assert world.esign.requests[0].signer_email == "db@x.example" and world.esign.find("s1") == pid


# --- router ---------------------------------------------------------------------------------------------

def test_router_sends_each_action_type_to_its_owner(world) -> None:
    router = world.router
    router.perform({"type": "first_touch", "idempotency_key": "e1", "to_email": "a@x.example",
                    "payload": {"subject": "S", "body": "B"}})
    router.perform({"type": "crm_update", "idempotency_key": "c1",
                    "payload": {"kind": "deals", "properties": {}}})
    assert len(world.mailer.sent) == 1 and world.crm.find("c1")
    assert router.find("e1") == message_id_for("e1") and router.find("c1") and router.find("zz") is None


def test_router_refuses_an_unregistered_action_type(world) -> None:
    with pytest.raises(ValueError, match="no provider"):
        ActionRouter({}).perform({"type": "wire_transfer"})


def test_fixture_loader_reads_yaml_data() -> None:
    assert load("calendar")["busy"]
