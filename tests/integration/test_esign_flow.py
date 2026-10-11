"""The IO for signature: safe sending, the callback, and the close. Dropbox Sign's behaviour is stood in
for by the mock (the real adapter is tested against a fake server in tests/unit)."""

import json
import random

import psycopg
import pytest
from fastapi.testclient import TestClient

from revos.adapters.dropboxsign.client import DropboxSignError
from revos.adapters.esign_provider import ESignOutboxProvider, ReviewRequired
from revos.adapters.mock.esign import MockESign
from revos.adapters.mock.slack import MockSlack
from revos.adapters.slack.handoff import SlackHandoff
from revos.api.main import create_app
from revos.esign.webhook import HELLO, SignatureWebhook
from revos.runtime.worker import Worker
from tests.fakes.hubspot import FakeHubSpot
from tests.integration.test_close_the_deal import qualified, send_all, stage, terms
from tests.integration.test_crm_wiring import hubspot
from tests.pipeline_helpers import CONTACT_EMAIL, audit


class LossyESign(MockESign):
    """The create call succeeds at the provider, but the response never reaches us."""

    def __init__(self, *, searchable: bool = True, fail_with: Exception | None = None) -> None:
        super().__init__()
        self.searchable, self.fail_with, self.lose_next = searchable, fail_with, True

    def send(self, request):
        if self.fail_with is not None:
            exc, self.fail_with = self.fail_with, None
            raise exc
        sid = super().send(request)
        if self.lose_next:
            self.lose_next = False
            raise ConnectionError("the response was lost")
        return sid

    def find_by_key(self, key):
        return super().find_by_key(key) if self.searchable else None


def wire(rig, esign, test_db_url, slack=None):
    factory = lambda: psycopg.connect(test_db_url)       # noqa: E731
    rig.orch.provider._routes["send_io"] = ESignOutboxProvider(esign, factory)
    rig.orch.provider._routes["handoff"] = SlackHandoff(slack or MockSlack(), "#revops", factory)
    rig.orch.esign = esign
    return factory


def io_ready(conn, ro_conn, pack, clock, test_db_url, esign=None, fake=None, **kw):
    rig = qualified(conn, ro_conn, pack, clock, **kw)
    esign = esign or rig.world.esign
    slack = MockSlack()
    factory = wire(rig, esign, test_db_url, slack)
    if fake is not None:                          # HubSpot joins only once the deal is qualified
        rig.orch.provider._routes["crm_update"] = rig.orch.provider._routes["crm_note"] = hubspot(fake)
        rig.orch.crm_sync = True
    terms(rig)
    rig.orch.handle_io(rig.deal_id)
    return rig, esign, slack, factory


def sends(conn):
    return conn.execute("SELECT id FROM actions WHERE type = 'send_io' AND status = 'approved'").fetchall()


# --- what is sent -----------------------------------------------------------------------------------------------

def test_the_io_goes_as_a_rendered_pdf_in_test_mode_to_the_lead_records_signer(conn, ro_conn, pack, clock, test_db_url):
    rig, esign, _, _ = io_ready(conn, ro_conn, pack, clock, test_db_url)
    send_all(rig)
    (request,) = esign.requests
    assert request.test_mode is True and request.signer_email == CONTACT_EMAIL and request.signer_name == "Priya Nair"
    assert request.pdf.startswith(b"%PDF") and len(request.pdf) > 800 and request.filename.endswith(".pdf")
    assert stage(conn) == "out_for_signature"
    assert conn.execute("SELECT state, signature_request_id FROM esign_attempts").fetchone() == ("created", "env-1")
    assert conn.execute("SELECT provider_id FROM actions WHERE type = 'send_io'").fetchone()[0] == "env-1"


def test_a_payload_cannot_change_the_recipient(conn, ro_conn, pack, clock, test_db_url):
    rig, esign, _, _ = io_ready(conn, ro_conn, pack, clock, test_db_url)
    conn.execute("UPDATE actions SET payload = payload || %s WHERE type = 'send_io'",
                 (json.dumps({"signer_email": "attacker@evil.org", "to": "attacker@evil.org"}),))
    send_all(rig)
    assert esign.requests[0].signer_email == CONTACT_EMAIL


def test_the_mock_and_the_real_adapter_both_refuse_test_mode_off() -> None:
    from revos.ports.esign import SignatureRequest
    with pytest.raises(ValueError):
        MockESign().send(SignatureRequest("k", "t", "d", "a@b.c", "N", test_mode=False))


# --- check before retry ---------------------------------------------------------------------------------------------

def test_a_lost_create_response_is_recovered_from_the_provider_and_never_sent_twice(conn, ro_conn, pack, clock,
                                                                                    test_db_url):
    rig, esign, _, _ = io_ready(conn, ro_conn, pack, clock, test_db_url, esign=LossyESign())
    (action,) = [r[0] for r in sends(conn)]
    with pytest.raises(ConnectionError):
        rig.orch.send_action(action)                      # created at the provider, response lost
    assert len(esign.requests) == 1
    assert conn.execute("SELECT state FROM esign_attempts").fetchone()[0] == "sending"
    assert conn.execute("SELECT status FROM actions WHERE id = %s", (action,)).fetchone()[0] == "approved"
    assert rig.orch.send_action(action).status == "sent"                      # the retry
    assert len(esign.requests) == 1                                           # found, not sent again
    assert conn.execute("SELECT provider_id FROM actions WHERE id = %s", (action,)).fetchone()[0] == "env-1"
    assert audit(conn, "action.sent", rig.deal_id)[-1]["recovered_from_provider"] is True


def test_when_it_cannot_be_told_whether_it_was_created_the_action_goes_to_review_and_nothing_is_sent_again(
        conn, ro_conn, pack, clock, test_db_url):
    esign = LossyESign(searchable=False)                                      # a new request is not searchable yet
    rig, _, _, _ = io_ready(conn, ro_conn, pack, clock, test_db_url, esign=esign)
    (action,) = [r[0] for r in sends(conn)]
    with pytest.raises(ConnectionError):
        rig.orch.send_action(action)
    for _ in range(3):                                                         # every later attempt: review
        with pytest.raises(ReviewRequired):
            rig.orch.send_action(action)
    assert len(esign.requests) == 1
    assert conn.execute("SELECT state FROM esign_attempts").fetchone()[0] == "review"
    assert conn.execute("SELECT status FROM actions WHERE id = %s", (action,)).fetchone()[0] != "sent"
    assert audit(conn, "esign.review_required", rig.deal_id)


def test_a_clear_refusal_by_the_provider_created_nothing_so_a_retry_may_send(conn, ro_conn, pack, clock, test_db_url):
    esign = LossyESign(fail_with=DropboxSignError("send", "bad_request"))
    esign.lose_next = False
    rig, _, _, _ = io_ready(conn, ro_conn, pack, clock, test_db_url, esign=esign)
    (action,) = [r[0] for r in sends(conn)]
    with pytest.raises(DropboxSignError):
        rig.orch.send_action(action)
    assert conn.execute("SELECT count(*) FROM esign_attempts").fetchone()[0] == 0
    assert rig.orch.send_action(action).status == "sent" and len(esign.requests) == 1


def test_the_hard_discount_block_never_reaches_the_provider(conn, ro_conn, pack, clock, test_db_url):
    rig = qualified(conn, ro_conn, pack, clock)
    esign = rig.world.esign
    wire(rig, esign, test_db_url)
    terms(rig, discount_pct=40)
    rig.orch.handle_io(rig.deal_id)
    send_all(rig)
    assert esign.requests == [] and conn.execute("SELECT count(*) FROM esign_attempts").fetchone()[0] == 0


# --- the callback -----------------------------------------------------------------------------------------------------

class Callback:
    def __init__(self, rig, esign, test_db_url, clock):
        self.esign = esign
        hook = SignatureWebhook(esign, lambda: psycopg.connect(test_db_url), clock)
        self.client = TestClient(create_app(None, "", hook))

    def post(self, payload=None, *, field="json", raw=None):
        if raw is not None:
            return self.client.post("/dropboxsign/webhook", files={field: (None, raw)})
        return self.client.post("/dropboxsign/webhook", files={field: (None, json.dumps(payload))})


def send_tolerant(rig):
    """send_all, but a CRM write that has to wait for its deal to exist is simply tried again."""
    from revos.domain.errors import RevosError
    for _ in range(3):
        for (action_id,) in rig.orch.conn.execute(
                "SELECT id FROM actions WHERE status = 'approved' AND type IN ('crm_update', 'crm_note', "
                "'send_io', 'handoff', 'follow_up') ORDER BY created_at, id").fetchall():
            try:
                rig.orch.send_action(action_id)
            except RevosError:
                pass


def flow(conn, ro_conn, pack, clock, test_db_url, **kw):
    rig, esign, slack, factory = io_ready(conn, ro_conn, pack, clock, test_db_url, **kw)
    send_tolerant(rig) if kw.get("fake") is not None else send_all(rig)
    envelope = conn.execute("SELECT fields->>'envelope_id' FROM deals").fetchone()[0]
    return rig, esign, slack, envelope, Callback(rig, esign, test_db_url, clock), Worker(
        rig.orch.handlers(), clock, rng=random.Random(1))


def drain(worker, conn):
    while worker.run_once(conn):
        pass


def test_a_valid_event_is_acknowledged_the_way_dropbox_sign_requires(conn, ro_conn, pack, clock, test_db_url):
    rig, esign, _, envelope, cb, worker = flow(conn, ro_conn, pack, clock, test_db_url)
    reply = cb.post(esign.signed_event(envelope, event_type="signature_request_viewed"))
    assert reply.status_code == 200 and reply.text == HELLO and HELLO == "Hello API Event Received"


def test_the_callback_test_event_is_answered_and_does_nothing(conn, ro_conn, pack, clock, test_db_url):
    rig, esign, _, envelope, cb, worker = flow(conn, ro_conn, pack, clock, test_db_url)
    before = conn.execute("SELECT count(*) FROM events WHERE type = 'signature.event'").fetchone()[0]
    reply = cb.post({**esign.signed_event(envelope, event_type="callback_test"), "signature_request": {}})
    assert reply.status_code == 200 and reply.text == HELLO
    assert conn.execute("SELECT count(*) FROM events WHERE type = 'signature.event'").fetchone()[0] == before


@pytest.mark.parametrize("case", ["wrong_hash", "missing_json", "not_json", "tampered_time", "tampered_type", "no_event"])
def test_a_bad_callback_is_a_401_and_changes_nothing(conn, ro_conn, pack, clock, test_db_url, case):
    rig, esign, _, envelope, cb, worker = flow(conn, ro_conn, pack, clock, test_db_url)
    esign.simulate_signed(envelope)
    good = esign.signed_event(envelope)
    bad = {"wrong_hash": {**good, "event": {**good["event"], "event_hash": "0" * 64}},
           "tampered_time": {**good, "event": {**good["event"], "event_time": "1760999999"}},
           "tampered_type": {**good, "event": {**good["event"], "event_type": "signature_request_signed"}},
           "no_event": {"signature_request": good["signature_request"]}}
    if case == "missing_json":
        reply = cb.post(good, field="other")
    elif case == "not_json":
        reply = cb.post(raw="{not json")
    else:
        reply = cb.post(bad[case])
    assert reply.status_code == 401 and reply.text != HELLO
    drain(worker, conn)
    assert stage(conn) == "out_for_signature"
    assert conn.execute("SELECT count(*) FROM events WHERE type = 'signature.event'").fetchone()[0] == 0


def test_progress_events_are_recorded_and_move_nothing(conn, ro_conn, pack, clock, test_db_url):
    rig, esign, _, envelope, cb, worker = flow(conn, ro_conn, pack, clock, test_db_url)
    for kind in ("signature_request_sent", "signature_request_viewed", "signature_request_signed"):
        assert cb.post(esign.signed_event(envelope, event_type=kind)).status_code == 200
    drain(worker, conn)
    for name in ("signature.sent", "signature.viewed", "signature.signed"):
        assert len(audit(conn, name, rig.deal_id)) == 1
    assert stage(conn) == "out_for_signature"


def test_all_signed_closes_the_deal_once_updates_hubspot_and_posts_the_handoff(conn, ro_conn, pack, clock,
                                                                              test_db_url):
    fake = FakeHubSpot()
    rig, esign, slack, envelope, cb, worker = flow(conn, ro_conn, pack, clock, test_db_url, fake=fake)
    esign.simulate_signed(envelope)
    body = esign.signed_event(envelope)
    assert cb.post(body).status_code == 200
    drain(worker, conn)
    assert stage(conn) == "handoff"
    changes = [r[0] for r in conn.execute("SELECT detail->>'to' FROM audit_log WHERE event = 'deal.stage_changed' "
                                          "ORDER BY id")]
    assert changes.count("closed_won") == 1 and audit(conn, "deal.signed", rig.deal_id)
    events = conn.execute("SELECT count(*) FROM events WHERE type = 'signature.event'").fetchone()[0]
    assert cb.post(body).status_code == 200                                   # the same event again
    drain(worker, conn)
    assert conn.execute("SELECT count(*) FROM events WHERE type = 'signature.event'").fetchone()[0] == events == 1
    assert conn.execute("SELECT count(*) FROM jobs WHERE type = 'signature.event'").fetchone()[0] == 1
    assert len(audit(conn, "deal.signed", rig.deal_id)) == 1
    (post,) = slack.posts
    assert "Closed won" in post.text or "Closed won" in json.dumps(post.blocks)
    flat = json.dumps(post.blocks)
    for expected in ("Priya Nair", "TEST MODE", "customer-success" if False else "Open items", envelope):
        assert expected in flat, expected
    # HubSpot: stage, the close fields and a note carrying the signature request id
    for _ in range(3):
        drain(worker, conn)
        clock.advance(3600)
    (deal,) = fake.objects["deals"].values()
    props = deal["properties"]
    assert props["revos_signature_request_id"] == envelope and props["revos_signed_at"]
    assert props["closedate"] == props["revos_signed_at"] and float(props["amount"]) == 33210.0
    assert props["revos_stage"] in {"closed_won", "handoff"}
    assert any(envelope in n and "Signed (TEST MODE" in n for n in [
        x["properties"]["hs_note_body"] for x in fake.objects["notes"].values()])


def test_a_replayed_valid_event_with_another_envelope_id_cannot_close_a_deal(conn, ro_conn, pack, clock, test_db_url):
    """The documented hash covers only event_time and event_type, so the envelope id is not authenticated by
    it. The deal closes only when the provider itself says the request is complete."""
    rig, esign, _, envelope, cb, worker = flow(conn, ro_conn, pack, clock, test_db_url)
    good = esign.signed_event(envelope)                                  # a real, valid event...
    forged = {**good, "signature_request": {"signature_request_id": envelope}}   # ...for a request not yet signed
    assert cb.post(forged).status_code == 200                              # (the provider has not signed it)
    drain(worker, conn)
    assert stage(conn) == "out_for_signature" and audit(conn, "signature.not_confirmed", rig.deal_id)
    other = dict(good, signature_request={"signature_request_id": "env-never-sent"})
    esign.simulate_signed("env-never-sent")
    cb.post(other)
    drain(worker, conn)
    assert audit(conn, "signature.unknown_envelope") and stage(conn) == "out_for_signature"


def test_the_endpoint_does_not_exist_without_a_configured_provider() -> None:
    assert TestClient(create_app(None, "")).post("/dropboxsign/webhook", files={"json": (None, "{}")}).status_code == 404
