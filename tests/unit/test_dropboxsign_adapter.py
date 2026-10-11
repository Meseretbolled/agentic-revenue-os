"""The real Dropbox Sign adapter against a fake server: test mode only, retries, search, events."""

import hashlib
import hmac
from datetime import UTC, datetime

import httpx
import pytest

from revos.adapters.dropboxsign.client import (
    BASE,
    METADATA_KEY,
    DropboxSignAdapter,
    DropboxSignError,
    DropboxSignUnavailable,
    TestModeViolation,
    key_token,
)
from revos.domain.errors import WebhookRejected
from revos.ports.esign import SignatureRequest

KEY = "ds-FAKE-API-KEY-FOR-TESTS-0123456789"
PDF = b"%PDF-1.3 fake"


def request(**kw) -> SignatureRequest:
    return SignatureRequest(**{"idempotency_key": "k1", "title": "IO", "document": "d", "signer_email": "s@x.org",
                               "signer_name": "Sam", "pdf": PDF, **kw})


class FakeDropbox:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    def client(self):
        def handler(req: httpx.Request) -> httpx.Response:
            self.requests.append(req)
            status, body, *_ = self.replies.pop(0)
            return httpx.Response(status, json=body)
        return httpx.Client(transport=httpx.MockTransport(handler))


def adapter(replies):
    fake = FakeDropbox(replies)
    return DropboxSignAdapter(KEY, http=fake.client(), sleep=lambda s: None), fake


def created(sid="sr1", test_mode=True):
    return (200, {"signature_request": {"signature_request_id": sid, "test_mode": test_mode}})


def test_a_send_is_basic_auth_multipart_test_mode_with_our_key_in_the_metadata() -> None:
    ds, fake = adapter([created()])
    assert ds.send(request()) == "sr1"
    (sent,) = fake.requests
    assert str(sent.url) == f"{BASE}/signature_request/send" and sent.method == "POST"
    assert sent.headers["authorization"].startswith("Basic ")          # the key as the user, no password
    assert sent.headers["content-type"].startswith("multipart/form-data")
    body = sent.content
    for part in (b'name="test_mode"\r\n\r\n1', b'name="signers[0][email_address]"\r\n\r\ns@x.org',
                 b'name="signers[0][name]"\r\n\r\nSam', f'name="metadata[{METADATA_KEY}]"'.encode(),
                 key_token("k1").encode(), b'name="files[0]"; filename="insertion-order.pdf"', PDF):
        assert part in body, part
    assert KEY.encode() not in body and "DropboxSignAdapter(test_mode=True)" == repr(ds)


def test_a_request_with_test_mode_off_is_refused_before_any_network_call() -> None:
    ds, fake = adapter([created()])
    with pytest.raises(TestModeViolation):
        ds.send(request(test_mode=False))
    assert fake.requests == []


def test_a_response_that_does_not_confirm_test_mode_is_cancelled_and_refused() -> None:
    ds, fake = adapter([created("live1", test_mode=False), (200, {})])
    with pytest.raises(TestModeViolation):
        ds.send(request())
    assert [str(r.url) for r in fake.requests] == [f"{BASE}/signature_request/send",
                                                  f"{BASE}/signature_request/cancel/live1"]


def test_a_send_is_never_retried_automatically_because_it_may_have_been_created() -> None:
    ds, fake = adapter([(503, {})])
    with pytest.raises(DropboxSignUnavailable):
        ds.send(request())
    assert len(fake.requests) == 1


def test_a_client_error_is_permanent_and_carries_no_secret() -> None:
    ds, fake = adapter([(403, {"error": {"error_name": "forbidden", "error_msg": f"mine {KEY}"}})])
    with pytest.raises(DropboxSignError) as caught:
        ds.send(request())
    assert caught.value.code == "forbidden" and KEY not in str(caught.value)


def test_reads_are_retried_on_rate_limits_and_server_errors() -> None:
    ds, fake = adapter([(429, {}), (503, {}), (200, {"signature_request": {"is_complete": True}})])
    assert ds.status("sr1") == "signed" and len(fake.requests) == 3
    down, _ = adapter([(502, {})] * 3)
    with pytest.raises(DropboxSignUnavailable):
        down.status("sr1")


def test_status_is_signed_only_when_the_provider_says_complete() -> None:
    ds, _ = adapter([(200, {"signature_request": {"is_complete": False}})])
    assert ds.status("sr1") == "awaiting"


def listing(*items, pages=1):
    return (200, {"signature_requests": list(items), "list_info": {"num_pages": pages}})


def item(sid, key):
    return {"signature_request_id": sid, "metadata": {METADATA_KEY: key_token(key)}}


def test_a_request_is_found_by_the_metadata_we_attached() -> None:
    ds, fake = adapter([listing(item("a", "other"), item("sr9", "k1"))])
    assert ds.find_by_key("k1") == "sr9"
    assert fake.requests[0].url.params["query"] == f"metadata:{key_token('k1')}"


def test_when_the_search_index_lags_the_plain_list_is_checked_too() -> None:
    ds, fake = adapter([listing(), listing(item("sr9", "k1"))])
    assert ds.find_by_key("k1") == "sr9"
    assert "query" in fake.requests[0].url.params and "query" not in fake.requests[1].url.params


def test_a_key_that_nobody_carries_is_not_found_and_a_foreign_item_is_not_mistaken_for_it() -> None:
    ds, _ = adapter([listing(item("x", "different")), listing(item("y", "another"))])
    assert ds.find_by_key("k1") is None


# --- the callback, exactly as documented: HMAC-SHA256(api key, event_time + event_type), hex -------------------

def event(kind="signature_request_all_signed", sid="sr1", when="1760000000", key=KEY, hash_=None):
    digest = hash_ or hmac.new(key.encode(), (when + kind).encode(), hashlib.sha256).hexdigest()
    return {"event": {"event_type": kind, "event_time": when, "event_hash": digest},
            "signature_request": {"signature_request_id": sid}}


@pytest.mark.parametrize("kind,expected", [
    ("signature_request_sent", "sent"), ("signature_request_viewed", "viewed"),
    ("signature_request_signed", "signed"), ("signature_request_all_signed", "all_signed"),
    ("signature_request_downloadable", "other"), ("callback_test", "test")])
def test_a_valid_event_is_read_by_kind(kind, expected) -> None:
    ds, _ = adapter([])
    got = ds.parse_event(event(kind))
    assert got.kind == expected and got.envelope_id == "sr1" and got.event_type == kind
    assert got.event_id == f"sr1:{kind}:1760000000" and got.occurred_at == datetime.fromtimestamp(1760000000, UTC)


def test_the_hash_matches_what_openssl_would_print() -> None:
    # echo -n $event_time$event_type | openssl dgst -sha256 -hmac $apikey
    ds, _ = adapter([])
    expected = hmac.new(KEY.encode(), b"1760000000signature_request_signed", hashlib.sha256).hexdigest()
    assert ds.parse_event(event("signature_request_signed", hash_=expected.upper())).kind == "signed"


@pytest.mark.parametrize("bad", [
    event(key="a-different-key"),                          # signed with the wrong key
    event(hash_="0" * 64), {**event(), "event": {**event()["event"], "event_hash": ""}},
    {**event(), "event": {**event()["event"], "event_time": "1760000001"}},     # tampered time
    {**event(), "event": {**event()["event"], "event_type": "signature_request_signed"}},   # tampered type
    {"event": {"event_type": "x"}}, {}, {"event": "nope"}])
def test_a_wrong_missing_or_tampered_hash_is_rejected(bad) -> None:
    ds, _ = adapter([])
    with pytest.raises(WebhookRejected):
        ds.parse_event(bad)


def test_an_event_naming_no_signature_request_is_rejected_except_the_callback_test() -> None:
    ds, _ = adapter([])
    nameless = event()
    nameless["signature_request"] = {}
    with pytest.raises(WebhookRejected):
        ds.parse_event(nameless)
    assert ds.parse_event({**event("callback_test"), "signature_request": {}}).kind == "test"
