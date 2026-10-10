"""HubSpot as the outbox provider: every write has an idempotency key and a check-before-retry."""

import httpx
import pytest

from revos.adapters.hubspot.client import HubSpotClient
from revos.adapters.hubspot.crm import HubSpotCRM
from revos.adapters.router import ActionRouter
from revos.runtime.outbox import enqueue_action, execute_action
from tests.fakes.hubspot import TOKEN, FakeHubSpot
from tests.pipeline_helpers import make_lead


@pytest.fixture
def world(conn, clock):
    fake = FakeHubSpot()
    crm = HubSpotCRM(HubSpotClient(TOKEN, http=fake.client(), sleep=lambda s: None))
    router = ActionRouter({"crm_update": crm, "crm_note": crm})
    deal, contact = make_lead(conn)
    return fake, crm, router, deal, contact


def approved(conn, key, type_, payload, deal, contact):
    action = enqueue_action(conn, key, type_, payload, autonomy_level="auto", deal_id=deal, contact_id=contact)
    conn.execute("UPDATE actions SET status = 'approved', decision = 'approved', decision_by = 'policy', "
                 "decided_at = now() WHERE id = %s", (action,))
    return action


class DiesAfterTheWrite:
    """The write reaches HubSpot, then our process 'dies' before the outbox records it."""

    def __init__(self, inner):
        self.inner, self.armed = inner, True

    def find(self, key):
        return self.inner.find(key)

    def perform(self, action):
        result = self.inner.perform(action)
        if self.armed:
            self.armed = False
            raise ConnectionError("process died after HubSpot accepted the write")
        return result


def test_a_deal_write_goes_through_the_outbox_and_is_recorded(conn, clock, world) -> None:
    fake, crm, router, deal, contact = world
    action = approved(conn, "w1", "crm_update", {"kind": "deals", "stage": "scored",
                                                 "properties": {"dealname": "Brightside"}}, deal, contact)
    pid = execute_action(conn, action, router, clock)
    assert fake.objects["deals"][pid]["properties"]["revos_deal_id"] == str(deal)
    assert conn.execute("SELECT status, provider_id FROM actions WHERE id = %s", (action,)).fetchone() == (
        "sent", pid)


def test_a_crash_after_hubspot_saved_the_record_does_not_create_a_second_one(conn, clock, world) -> None:
    fake, crm, _, deal, contact = world
    router = ActionRouter({"crm_update": DiesAfterTheWrite(crm)})
    action = approved(conn, "w2", "crm_update", {"kind": "deals", "stage": "scored", "properties": {}},
                      deal, contact)
    with pytest.raises(ConnectionError):
        execute_action(conn, action, router, clock)
    assert conn.execute("SELECT status FROM actions WHERE id = %s", (action,)).fetchone()[0] == "approved"
    pid = execute_action(conn, action, router, clock)                       # the retry
    assert len(fake.objects["deals"]) == 1 and pid in fake.objects["deals"]
    detail = conn.execute("SELECT detail FROM audit_log WHERE event = 'action.sent'").fetchone()[0]
    assert detail["recovered_from_provider"] is True
    assert sum(1 for m, p in fake.calls if m == "POST" and p == "/crm/v3/objects/deals") == 1


def test_a_note_is_written_once_even_when_the_retry_comes_after_a_crash(conn, clock, world) -> None:
    fake, crm, _, deal, contact = world
    crm.upsert_deal(str(deal), {}, "seed")
    router = ActionRouter({"crm_note": DiesAfterTheWrite(crm)})
    action = approved(conn, "n1", "crm_note", {"record_id": str(deal), "text": "Call brief ..."}, deal, contact)
    with pytest.raises(ConnectionError):
        execute_action(conn, action, router, clock)
    execute_action(conn, action, router, clock)
    assert len(fake.objects["notes"]) == 1


def test_the_contact_is_written_to_the_address_on_the_contact_row(conn, clock, world) -> None:
    fake, crm, router, deal, contact = world
    action = approved(conn, "c1", "crm_update", {"kind": "contacts", "email": "attacker@evil.test",
                                                 "properties": {"firstname": "Sam"}}, deal, contact)
    pid = execute_action(conn, action, router, clock)
    assert fake.objects["contacts"][pid]["properties"]["email"] == "sam.lee@brightside-optical.example"


def test_an_unapproved_write_never_reaches_hubspot(conn, clock, world) -> None:
    from revos.domain.errors import ActionNotApproved
    fake, crm, router, deal, contact = world
    action = enqueue_action(conn, "u1", "crm_update", {"kind": "deals", "properties": {}},
                            autonomy_level="needs_approval", deal_id=deal, contact_id=contact)
    with pytest.raises(ActionNotApproved):
        execute_action(conn, action, router, clock)
    assert fake.calls == []


def test_a_permanent_error_from_hubspot_surfaces_for_the_queue_to_dead_letter(conn, clock, world) -> None:
    from revos.domain.errors import PermanentError
    fake, crm, router, deal, contact = world
    fake.token = "something-else"                                          # the key stops working
    action = approved(conn, "p1", "crm_update", {"kind": "deals", "properties": {}}, deal, contact)
    with pytest.raises(PermanentError):
        execute_action(conn, action, router, clock)
    assert conn.execute("SELECT status FROM actions WHERE id = %s", (action,)).fetchone()[0] == "approved"
    assert httpx and len(fake.objects["deals"]) == 0
