"""The real Fireflies adapter against a fake GraphQL server: no network, no key."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from revos.adapters.fireflies.client import (
    ENDPOINT,
    FirefliesAdapter,
    FirefliesError,
    FirefliesUnavailable,
    parse_date,
)
from revos.api.main import create_app
from revos.composition import build_transcripts
from revos.modes import fireflies_configured, integration_modes

KEY = "ff-FAKE-KEY-FOR-TESTS-12345"


class FakeFireflies:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    def client(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append((str(request.url), json.loads(request.content), dict(request.headers)))
            status, body, *rest = self.replies.pop(0)
            return httpx.Response(status, json=body, headers=rest[0] if rest else {})
        return httpx.Client(transport=httpx.MockTransport(handler))


def adapter(replies, sleeps=None):
    fake = FakeFireflies(replies)
    return FirefliesAdapter(KEY, http=fake.client(), sleep=(sleeps.append if sleeps is not None else lambda s: None)), fake


LISTING = {"data": {"transcripts": [
    {"id": "a1", "title": "Harborview Optical Collective - intro call", "date": 1792076400000.0,
     "dateString": "2026-10-15T15:00:00.000Z"},
    {"id": "a2", "title": "Other", "date": 1791990000000, "dateString": None}, None, {"title": "no id"}]}}
ONE = {"data": {"transcript": {
    "id": "a1", "title": "Intro", "date": 1792076400000.0, "dateString": "2026-10-15T15:00:00.000Z",
    "participants": ["jo@x.example", "maya@y.example", None],
    "sentences": [{"index": 0, "speaker_name": "Jo", "text": "Hello.", "start_time": 0.5, "end_time": 1.5},
                  {"index": 1, "speaker_name": None, "text": "  ", "start_time": 2, "end_time": 3},
                  {"index": 2, "speaker_name": "Maya", "text": "Our budget is large.", "start_time": 3,
                   "end_time": 4}],
    "summary": {"overview": "They discussed budget.", "action_items": "send deck"}}}}


def test_listing_returns_titles_and_dates_only_and_uses_the_bearer_key() -> None:
    ff, fake = adapter([(200, LISTING)])
    items = ff.list_recent()
    assert [(i.id, i.title) for i in items] == [("a1", "Harborview Optical Collective - intro call"),
                                                ("a2", "Other")]
    assert items[0].date == datetime(2026, 10, 15, 15, 0, tzinfo=UTC)
    assert items[1].date == datetime(2026, 10, 14, 15, 0, tzinfo=UTC)                      # epoch milliseconds
    url, body, headers = fake.requests[0]
    assert url == ENDPOINT and "transcripts(limit" in body["query"] and body["variables"] == {"limit": 25}
    assert "sentences" not in body["query"]                                             # a listing has no content
    assert headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in repr(ff)


def test_fetching_one_maps_participants_speaker_sentences_and_the_summary() -> None:
    ff, fake = adapter([(200, ONE)])
    t = ff.get("a1")
    assert t.id == "a1" and t.participants == ("jo@x.example", "maya@y.example")
    assert [(s.index, s.speaker_name, s.text) for s in t.sentences] == [
        (0, "Jo", "Hello."), (2, "Maya", "Our budget is large.")]                     # the blank one is dropped
    assert t.text == "Jo: Hello.\nMaya: Our budget is large." and t.summary == "They discussed budget."
    assert fake.requests[0][1]["variables"] == {"id": "a1"}
    assert all(w in fake.requests[0][1]["query"] for w in ("speaker_name", "participants", "overview"))


def test_a_missing_transcript_is_none_and_list_ready_gives_ids() -> None:
    ff, _ = adapter([(200, {"data": {"transcript": None}}), (200, LISTING)])
    assert ff.get("nope") is None and ff.list_ready() == ["a1", "a2"]


def test_rate_limits_and_server_errors_are_retried_with_backoff() -> None:
    sleeps: list[float] = []
    ff, fake = adapter([(429, {}, {"Retry-After": "2"}), (503, {}),
                        (200, {"errors": [{"code": "too_many_requests"}]}), (200, LISTING)], sleeps)
    assert len(ff.list_recent()) == 2 and len(fake.requests) == 4 and 2.0 in sleeps
    down, _ = adapter([(502, {})] * 4)
    with pytest.raises(FirefliesUnavailable):
        down.list_recent()


@pytest.mark.parametrize("status,body,code", [
    (200, {"errors": [{"message": f"bad key {KEY}", "code": "auth_failed"}]}, "auth_failed"),
    (200, {"errors": [{"message": "x", "extensions": {"code": "object_not_found"}}]}, "object_not_found"),
    (401, {}, "http_401"), (403, {"detail": KEY}, "http_403"), (404, {}, "http_404")])
def test_other_client_errors_are_permanent_and_carry_no_secret(status, body, code) -> None:
    ff, fake = adapter([(status, body)])
    with pytest.raises(FirefliesError) as caught:
        ff.get("a1")
    assert caught.value.code == code and KEY not in str(caught.value) and len(fake.requests) == 1


def test_a_network_failure_is_unavailable_not_a_crash() -> None:
    def boom(request):
        raise httpx.ConnectError("down")
    ff = FirefliesAdapter(KEY, http=httpx.Client(transport=httpx.MockTransport(boom)), sleep=lambda s: None)
    with pytest.raises(FirefliesUnavailable):
        ff.list_ready()


def test_date_parsing_prefers_the_iso_string_and_falls_back_to_milliseconds() -> None:
    assert parse_date({"dateString": "2026-10-15T15:00:00Z", "date": 0}) == datetime(2026, 10, 15, 15, tzinfo=UTC)
    assert parse_date({"date": 1792076400000}) == datetime(2026, 10, 15, 15, tzinfo=UTC)
    assert parse_date({"dateString": "garbage", "date": 1792076400000}) == datetime(2026, 10, 15, 15, tzinfo=UTC)
    assert parse_date({}) == datetime.fromtimestamp(0, UTC)


def test_the_composition_root_picks_fireflies_by_its_key(monkeypatch) -> None:
    from revos.adapters.mock.transcripts import MockTranscripts
    mock = MockTranscripts()
    monkeypatch.delenv("FIREFLIES_API_KEY", raising=False)
    assert build_transcripts(mock) is mock and not fireflies_configured()
    monkeypatch.setenv("FIREFLIES_API_KEY", KEY)
    built = build_transcripts(mock)
    assert isinstance(built, FirefliesAdapter) and fireflies_configured()
    assert {m.service: m.mode for m in integration_modes()}["Fireflies"] == "REAL"
    assert KEY not in repr(built)


def test_there_is_no_webhook_endpoint_because_the_signature_scheme_is_not_fully_documented() -> None:
    client = TestClient(create_app(None, ""))
    assert client.post("/fireflies/webhook", json={"meetingId": "x"}).status_code == 404
