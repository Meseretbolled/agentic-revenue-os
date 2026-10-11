"""The real Slack adapter (over a fake HTTP server), the signature scheme, Block Kit views, health."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from revos.adapters.slack.client import SlackAdapter, SlackError, SlackUnavailable
from revos.api.main import create_app
from revos.slack.blocks import ActionView, render
from revos.slack.interactions import BadSignature, parse_press, verify_signature
from tests.slack_helpers import SECRET, sign

TOKEN = "xoxb-FAKE-TOKEN-FOR-TESTS-123456"
NOW = 1_760_000_000


class FakeSlackApi:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    def client(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append((request.url.path, json.loads(request.content), dict(request.headers)))
            status, body, headers = (self.replies.pop(0) + ({},))[:3]
            return httpx.Response(status, json=body, headers=headers)
        return httpx.Client(transport=httpx.MockTransport(handler))


def adapter(replies, sleeps=None):
    api = FakeSlackApi(replies)
    return SlackAdapter(TOKEN, http=api.client(), sleep=(sleeps.append if sleeps is not None else lambda s: None)), api


# --- the real adapter ---------------------------------------------------------------------------------------------

def test_post_update_and_ephemeral_use_the_right_methods_and_never_show_the_token() -> None:
    slack, api = adapter([(200, {"ok": True, "channel": "C123", "ts": "1.2"}), (200, {"ok": True}),
                          (200, {"ok": True})])
    posted = slack.post("#revops", "hi", [{"type": "divider"}])
    slack.update(posted.channel, posted.ts, "hi2", [])
    slack.ephemeral("C123", "U1", "psst")
    assert (posted.channel, posted.ts) == ("C123", "1.2")                  # the channel ID, for updating
    assert [p for p, _, _ in api.requests] == ["/api/chat.postMessage", "/api/chat.update",
                                              "/api/chat.postEphemeral"]
    assert api.requests[0][1] == {"channel": "#revops", "text": "hi", "blocks": [{"type": "divider"}],
                                  "unfurl_links": False, "unfurl_media": False}      # no link previews
    assert api.requests[1][1]["ts"] == "1.2" and api.requests[2][1] == {"channel": "C123", "user": "U1",
                                                                         "text": "psst"}
    assert all(h["authorization"] == f"Bearer {TOKEN}" for _, _, h in api.requests)
    assert TOKEN not in repr(slack)


@pytest.mark.parametrize("code", ["channel_not_found", "not_in_channel", "invalid_auth", "missing_scope"])
def test_a_permanent_slack_error_carries_only_its_code(code) -> None:
    slack, _ = adapter([(200, {"ok": False, "error": code, "detail": TOKEN})])
    with pytest.raises(SlackError) as caught:
        slack.post("#x", "t", [])
    assert caught.value.code == code and TOKEN not in str(caught.value)


def test_rate_limits_and_server_errors_are_retried_then_reported_as_unavailable() -> None:
    sleeps: list[float] = []
    slack, api = adapter([(429, {}, {"Retry-After": "3"}), (503, {}), (200, {"ok": True, "channel": "C", "ts": "9"})],
                         sleeps)
    assert slack.post("#x", "t", []).ts == "9" and len(api.requests) == 3 and 3.0 in sleeps
    down, _ = adapter([(503, {})] * 4)
    with pytest.raises(SlackUnavailable):
        down.post("#x", "t", [])


def test_a_transient_ok_false_is_unavailable_not_permanent() -> None:
    slack, _ = adapter([(200, {"ok": False, "error": "ratelimited"})])
    with pytest.raises(SlackUnavailable):
        slack.post("#x", "t", [])


# --- the signature -------------------------------------------------------------------------------------------------

def test_the_signature_scheme_is_slacks_v0_over_the_raw_body() -> None:
    raw = b"payload=%7B%22a%22%3A1%7D"
    ts = str(NOW)
    good = sign(raw, ts)
    verify_signature(SECRET, raw, ts, good, now=NOW)
    verify_signature(SECRET, raw, ts, good, now=NOW + 300)
    for args in ((raw, ts, good, NOW + 301), (raw, ts, good, NOW - 301), (raw + b" ", ts, good, NOW),
                 (raw, ts, None, NOW), (raw, None, good, NOW), (raw, "abc", good, NOW),
                 (raw, ts, good.replace("v0=", "v1="), NOW), (raw, ts, "", NOW)):
        with pytest.raises(BadSignature):
            verify_signature(SECRET, args[0], args[1], args[2], now=args[3])
    with pytest.raises(BadSignature):
        verify_signature("", raw, ts, good, now=NOW)


def test_a_known_slack_example_verifies() -> None:
    # the worked example from Slack's documentation of the v0 scheme
    secret = "8f742231b10e8888abcd99yyyzzz85a5"
    ts = "1531420618"
    body = (b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow&channel_id=G8PSS9T3V"
            b"&channel_name=foobar&user_id=U2CERLKJA&user_name=roadrunner&command=%2Fwebhook-collect"
            b"&text=&response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2FT1DC2JH3J%2F397700885554%2F96rGlfmibIGlgcZRskXaIFfN"
            b"&trigger_id=398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c")
    sig = "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503"
    verify_signature(secret, body, ts, sig, now=1531420618)


def test_parse_press_reads_only_an_action_id_and_who_pressed() -> None:
    from urllib.parse import urlencode
    payload = {"type": "block_actions", "user": {"id": "U1"}, "trigger_id": "t",
               "actions": [{"action_id": "revos_cancel", "value": "3f2504e0-4f89-11d3-9a0c-0305e82c3301"}]}
    press = parse_press(urlencode({"payload": json.dumps(payload)}).encode())
    assert press and press.verb == "cancel" and press.user_id == "U1"
    for bad in ({"type": "view_submission"}, {**payload, "actions": [{"action_id": "other", "value": "x"}]},
                {**payload, "actions": [{"action_id": "revos_cancel", "value": "not-a-uuid"}]}):
        assert parse_press(urlencode({"payload": json.dumps(bad)}).encode()) is None
    assert parse_press(b"garbage") is None


# --- Block Kit -------------------------------------------------------------------------------------------------------

def view(**kw):
    base = dict(action_id="3f2504e0-4f89-11d3-9a0c-0305e82c3301", action_type="first_touch", status="pending",
                decision=None, decision_by=None, hold_reason=None, lead="Maya at Acme", tier="A",
                total_score=83, score_reason="Tier A: 83\n  [fit] +30 big", subject="S", body="Hello",
                case_study=None, lesson_ids=[3, 5], claims=1, claim_check="1 claim(s) passed",
                seconds_left=42)
    base.update(kw)
    return ActionView(**base)


def buttons(blocks):
    return [e["text"]["text"] for b in blocks if b["type"] == "actions" for e in b["elements"]]


def test_each_state_shows_the_right_buttons_and_words() -> None:
    cases = {
        "pending": (view(), ["Cancel", "Cancel"], "in 42 seconds"),
        "awaiting": (view(status="awaiting_approval"), ["Approve", "Reject"] * 2, "Approval needed"),
        "sent": (view(status="sent", sent_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)), [],
                 "Sent 2026-01-02 03:04:05 UTC"),
        "cancelled": (view(status="cancelled", decision="vetoed", decision_by="slack:U9",
                           decided_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)), [], "Cancelled by <@U9>"),
        "rejected": (view(status="cancelled", decision="rejected", decision_by="slack:U9"), [],
                     "Rejected by <@U9>"),
        "blocked": (view(status="cancelled", decision="rejected", decision_by="policy",
                         hold_reason="claim check failed"), [], "Blocked: claim check failed"),
        "gate-cancelled": (view(status="cancelled", decision="approved", decision_by="policy:veto_window",
                                hold_reason="recipient_not_allowed"), [], "Blocked: recipient_not_allowed"),
    }
    for name, (v, expected_buttons, words) in cases.items():
        text, blocks = render(v)
        assert buttons(blocks) == expected_buttons, name
        assert words in json.dumps(blocks), name
        assert text and len(text) <= 150


def test_a_blocked_message_never_has_an_approve_button_whatever_the_view_says() -> None:
    _, blocks = render(view(status="cancelled", decision="rejected", decision_by="policy", hold_reason="x"))
    assert "Approve" not in json.dumps(blocks) and "approve" not in json.dumps(blocks).lower().replace(
        "cannot be approved", "")


def test_the_score_reason_and_lessons_are_shown_and_the_body_is_clipped() -> None:
    _, blocks = render(view(body="x" * 5000))
    flat = json.dumps(blocks, ensure_ascii=False)
    assert "L3, L5" in flat and "[fit] +30 big" in flat and "…" in flat and "x" * 700 not in flat


# --- health ---------------------------------------------------------------------------------------------------------

def test_healthz_and_health_answer_ok_without_a_database() -> None:
    client = TestClient(create_app(None, ""), base_url="http://localhost")
    for path in ("/healthz", "/health"):
        assert client.get(path).json() == {"status": "ok"}
    assert "integrations" in client.get("/status").json()
