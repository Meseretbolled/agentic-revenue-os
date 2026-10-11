"""Slack: the veto message, the signed interactions endpoint, approval buttons, and the digest."""

import json
import threading
from datetime import timedelta

import psycopg
import pytest

from revos.orchestrator.sweeps import digest
from revos.policy.service import release_veto_expired, veto_action
from revos.runtime.events import record_event
from tests.pipeline_helpers import CONTACT_EMAIL, audit
from tests.slack_helpers import CHANNEL, sign, slack_rig


@pytest.fixture
def s(conn, ro_conn, pack, clock, test_db_url):
    return slack_rig(conn, ro_conn, pack, clock, test_db_url)


def text_of(post) -> str:
    return "\n".join(str(b) for b in post.blocks)


def status(conn, action_id):
    return conn.execute("SELECT status, decision, decision_by FROM actions WHERE id = %s",
                        (action_id,)).fetchone()


def sent_count(conn) -> int:
    return conn.execute("SELECT count(*) FROM messages WHERE direction = 'outbound'").fetchone()[0]


# --- 1. what is posted ----------------------------------------------------------------------------------------

def action_blocks(post):
    return [(n, [e["text"]["text"] for e in b["elements"]]) for n, b in enumerate(post.blocks)
            if b["type"] == "actions"]


def test_an_auto_with_veto_action_is_posted_with_a_short_header_and_the_button_right_under_it(s):
    action = s.hot_first_touch()
    s.drain()
    (post,) = s.slack.posts
    body = text_of(post)
    header = post.blocks[0]["text"]["text"]
    assert post.channel == CHANNEL
    assert "Sending to Maya at Harborview Optical Collective in 60 seconds" in header and len(header) < 120
    assert post.blocks[1]["type"] == "actions"                        # the button directly under the header
    assert action_blocks(post)[0] == (1, ["Cancel"])
    assert post.blocks[1]["elements"][0]["value"] == str(action)
    assert CONTACT_EMAIL not in body and "@" not in body.replace("<@", "")         # the lead, not the address
    # compact summary right below: tier, score, the top reason, subject, case study
    summary = post.blocks[2]["text"]["text"]
    assert "Tier A · 83/100" in summary and "Subject:" in summary and "Scaling local campaigns" in summary
    assert "Ethos Veterinary Health (vendor-reported)" in summary and "[fit]" in summary
    # the detail is under the button, in small print
    assert any(b["type"] == "context" and "Why this lead" in json.dumps(b) and "Draft" in json.dumps(b)
               for b in post.blocks[3:])
    assert "Lessons cited" in body and "Claim check" in body
    # the same button again at the very bottom, and nowhere else
    assert [n for n, _ in action_blocks(post)] == [1, len(post.blocks) - 1]
    assert all(t == ["Cancel"] for _, t in action_blocks(post))
    assert len(post.blocks) <= 7


def test_the_cancel_button_is_in_the_first_two_blocks_for_every_veto_message(s):
    s.hot_first_touch()
    s.drain()
    (post,) = s.slack.posts
    assert any(b["type"] == "actions" for b in post.blocks[:2])


def test_the_message_is_updated_in_place_to_sent_with_the_time(s):
    s.hot_first_touch()
    s.drain()
    s.drain(61)
    assert sent_count(s.conn) == 1
    (post,) = s.slack.posts                                    # still one message
    body = text_of(post)
    assert "Sent 2026-10-12 09:01" in body and "first touch to Maya at Harborview" in body
    assert s.slack.buttons(post.ts) == [] and len(s.slack.updates) >= 1


def test_actions_that_are_not_veto_actions_are_not_posted(s):
    s.rig.orch.handle_lead_created(s.rig.deal_id)
    s.drain()
    assert all("crm" not in p.text.lower() for p in s.slack.posts) and len(s.slack.posts) == 1


# --- 2. the signature --------------------------------------------------------------------------------------------

def test_a_valid_signature_passes_and_cancels(s):
    action = s.hot_first_touch()
    s.drain()
    response, _ = s.press("cancel", action)
    assert response.status_code == 200 and status(s.conn, action)[:2] == ("cancelled", "vetoed")


@pytest.mark.parametrize("case", ["wrong_secret", "stale", "future", "missing_headers", "bad_signature",
                                  "no_prefix"])
def test_a_bad_stale_or_missing_signature_is_a_401_and_changes_nothing(s, case):
    import time
    action = s.hot_first_touch()
    s.drain()
    kw = {"wrong_secret": {"secret": "not-the-secret"}, "stale": {"timestamp": int(time.time()) - 330},
          "future": {"timestamp": int(time.time()) + 330}, "missing_headers": {"headers": False},
          "bad_signature": {"signature": "v0=" + "0" * 64}, "no_prefix": {"signature": "0" * 64}}[case]
    response, _ = s.press("cancel", action, **kw)
    assert response.status_code == 401 and response.content == b""
    assert status(s.conn, action)[0] == "pending"
    assert s.conn.execute("SELECT count(*) FROM slack_interactions").fetchone()[0] == 0


def test_the_signature_covers_the_raw_body_so_a_tampered_body_fails(s):
    action = s.hot_first_touch()
    s.drain()
    response, raw = s.press("cancel", action)
    assert response.status_code == 200
    import time
    ts = str(int(time.time()))
    other = raw.replace(b"U1", b"U2")
    bad = s.client.post("/slack/interactions", content=other, headers={
        "X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sign(raw, ts),
        "Content-Type": "application/x-www-form-urlencoded"})
    assert bad.status_code == 401


def test_a_request_with_no_signing_secret_configured_is_refused(conn, ro_conn, pack, clock, test_db_url):
    from fastapi.testclient import TestClient

    from revos.api.main import create_app
    app = create_app(None, "")
    response = TestClient(app).post("/slack/interactions", content=b"payload=%7B%7D", headers={
        "X-Slack-Request-Timestamp": "1", "X-Slack-Signature": "v0=00"})
    assert response.status_code == 401


def test_a_rejected_request_is_logged_without_its_body(s, tmp_path):
    from revos import obs
    log = obs.configure_logging(tmp_path / "l.jsonl")
    try:
        action = s.hot_first_touch()
        s.drain()
        s.press("cancel", action, secret="wrong", extra={"user": {"id": "UPRIVATEUSER", "username": "x"}})
        lines = [ln for ln in log.read_text().splitlines() if "interaction rejected" in ln]
        assert len(lines) == 1 and "signature mismatch" in lines[0]
        assert "UPRIVATEUSER" not in lines[0] and str(action) not in lines[0] and "payload" not in lines[0]
    finally:
        obs.disable_logging()


# --- 3. cancel and the window -------------------------------------------------------------------------------------

def test_cancel_stops_a_queued_send_and_audits_who_and_when(s):
    action = s.hot_first_touch()
    s.drain()
    response, _ = s.press("cancel", action, user="UALICE")
    assert response.status_code == 200
    assert status(s.conn, action) == ("cancelled", "vetoed", "slack:UALICE")
    (row,) = audit(s.conn, "slack.cancelled")
    assert row["user"] == "UALICE" and row["at"].startswith("2026-10-12T09:00") and row["action_id"] == str(action)
    s.drain()                                            # the sync job: the message changes
    assert "Cancelled by <@UALICE>" in text_of(s.post_for(action))
    s.drain(120)                                         # the window's timer fires: nothing is sent
    assert sent_count(s.conn) == 0 and status(s.conn, action)[0] == "cancelled"


def test_letting_the_window_expire_sends_exactly_once(s):
    action = s.hot_first_touch()
    s.drain()
    s.drain(61)
    s.drain(61)                                          # a second pass must change nothing
    assert sent_count(s.conn) == 1 and status(s.conn, action)[0] == "sent"
    response, _ = s.press("cancel", action)              # too late
    assert response.status_code == 200 and status(s.conn, action)[0] == "sent"
    assert "Too late" in s.slack.ephemerals[-1][2] and audit(s.conn, "slack.late")


def test_cancel_at_second_59_wins_and_at_second_60_loses(conn, ro_conn, pack, clock, test_db_url):
    for offset, expected in ((59, "cancelled"), (60, "pending")):
        conn.execute("TRUNCATE actions, deals, contacts, companies, events, jobs, audit_log, "
                     "slack_messages, slack_interactions CASCADE")
        s = slack_rig(conn, ro_conn, pack, clock, test_db_url)
        action = s.hot_first_touch()
        clock.advance(offset)
        s.press("cancel", action)
        assert status(conn, action)[0] == expected, offset


def test_cancel_then_the_timer_never_sends(s):
    action = s.hot_first_touch()
    s.press("cancel", action)
    assert release_veto_expired(s.conn, s.clock) == []
    s.drain(61)
    assert sent_count(s.conn) == 0


def test_the_timer_then_cancel_never_cancels_a_send_in_progress(s):
    action = s.hot_first_touch()
    s.clock.advance(61)
    assert release_veto_expired(s.conn, s.clock) == [action]          # approved by the window
    response, _ = s.press("cancel", action)
    assert response.status_code == 200 and status(s.conn, action)[0] == "approved"
    assert "already being sent" in s.slack.ephemerals[-1][2] or "Too late" in s.slack.ephemerals[-1][2]


class At:
    """A clock frozen at one instant, for one side of the race."""

    def __init__(self, moment):
        self.moment = moment

    def now(self):
        return self.moment


def test_cancel_and_the_send_racing_on_two_connections_have_exactly_one_winner(
        conn, ro_conn, pack, clock, test_db_url):
    wins = {"cancel": 0, "send": 0}
    for _ in range(12):
        conn.execute("TRUNCATE actions, deals, contacts, companies, events, jobs, audit_log, "
                     "slack_messages, slack_interactions CASCADE")
        s = slack_rig(conn, ro_conn, pack, clock, test_db_url)
        action = s.hot_first_touch()
        window_end = conn.execute("SELECT veto_until FROM actions WHERE id = %s", (action,)).fetchone()[0]
        barrier = threading.Barrier(2)
        results: dict[str, bool] = {}

        def cancel(action=action, window_end=window_end, barrier=barrier, results=results):
            with psycopg.connect(test_db_url, autocommit=True) as c:
                barrier.wait()
                try:                                   # a press just inside the window
                    veto_action(c, At(window_end - timedelta(seconds=1)), action, "slack:U1")
                    results["cancel"] = True
                except ValueError:
                    results["cancel"] = False

        def release(action=action, window_end=window_end, barrier=barrier, results=results):
            with psycopg.connect(test_db_url, autocommit=True) as c:
                barrier.wait()                         # the timer at the very end of the window
                results["send"] = action in release_veto_expired(c, At(window_end))

        threads = [threading.Thread(target=cancel), threading.Thread(target=release)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results["cancel"] != results["send"], results       # never both, never neither
        assert status(conn, action)[0] == ("cancelled" if results["cancel"] else "approved")
        wins["cancel" if results["cancel"] else "send"] += 1
    assert sum(wins.values()) == 12


# --- replay, forgery, users -----------------------------------------------------------------------------------------

def test_the_same_payload_delivered_twice_does_nothing_the_second_time_and_answers_200(s):
    action = s.hot_first_touch()
    s.drain()
    first, raw = s.press("cancel", action, trigger="same-trigger")
    again, _ = s.press("cancel", action, trigger="same-trigger")
    assert first.status_code == 200 and again.status_code == 200
    assert len(audit(s.conn, "slack.cancelled")) == 1 and len(audit(s.conn, "action.vetoed")) == 1
    assert s.conn.execute("SELECT count(*) FROM slack_interactions").fetchone()[0] == 1
    assert s.slack.ephemerals == []                                       # no second "too late" either


def test_a_replayed_approval_does_not_approve_twice(conn, ro_conn, pack, clock, test_db_url):
    s = slack_rig(conn, ro_conn, pack, clock, test_db_url)
    conn.execute("UPDATE system_settings SET value = '\"approval\"' WHERE key = 'autonomy_mode'")
    action = s.hot_first_touch()
    s.drain()
    s.press("approve", action, trigger="t-approve")
    s.press("approve", action, trigger="t-approve")
    s.drain()
    assert sent_count(conn) == 1 and len(audit(conn, "action.approved")) == 1


def test_a_hard_block_has_no_approve_button_and_a_forged_approve_is_refused(s):
    from revos.domain.enums import Tier
    from revos.policy.engine import ActionRequest
    from revos.policy.service import propose_action
    deal = s.rig.deal_id
    contact = s.rig.contact_id
    proposal = propose_action(
        s.conn, s.clock, s.rig.orch.policy_config(s.rig.orch._pack(s.rig.orch._deal(deal))),
        key="blocked-1", request=ActionRequest("first_touch", tier=Tier.A, body="hello",
                                               claim_check_passed=False),
        deal_id=deal, contact_id=contact,
        payload={"subject": "S", "body": "hello", "claims": []})
    action = proposal.action_id
    s.rig.orch._notify(action)
    s.drain()
    (post,) = s.slack.posts
    body = text_of(post)
    assert "Blocked:" in body and s.slack.buttons(post.ts) == []
    assert "cannot be approved or overridden" in body
    response, _ = s.press("approve", action, extra={"approved": True, "decision": "approved",
                                                     "actions": [{"action_id": "revos_approve",
                                                                  "value": str(action), "approved": True}]})
    assert response.status_code == 200
    assert status(s.conn, action)[:2] == ("cancelled", "rejected")           # untouched
    assert "blocked by policy" in s.slack.ephemerals[-1][2] and audit(s.conn, "slack.blocked")
    s.drain(120)
    assert sent_count(s.conn) == 0


def test_a_forged_approve_for_an_action_that_is_not_awaiting_approval_does_nothing(s):
    action = s.hot_first_touch()
    s.drain()
    response, _ = s.press("approve", action)                      # it is pending a veto, not awaiting approval
    assert response.status_code == 200 and status(s.conn, action)[0] == "pending"
    assert audit(s.conn, "slack.late")


def test_approval_mode_posts_approve_and_reject_and_the_buttons_work(conn, ro_conn, pack, clock, test_db_url):
    s = slack_rig(conn, ro_conn, pack, clock, test_db_url)
    conn.execute("UPDATE system_settings SET value = '\"approval\"' WHERE key = 'autonomy_mode'")
    action = s.hot_first_touch()
    s.drain()
    (post,) = s.slack.posts
    assert action_blocks(post)[0][1] == ["Approve", "Reject"] and post.blocks[1]["type"] == "actions"
    assert [t for _, t in action_blocks(post)] == [["Approve", "Reject"]] * 2      # top and bottom
    assert "Approval needed" in text_of(post) and status(conn, action)[0] == "awaiting_approval"
    s.drain(300)
    assert sent_count(conn) == 0                                  # nothing sends without a person
    response, _ = s.press("approve", action, user="UBOSS")
    assert response.status_code == 200 and status(conn, action) == ("approved", "approved", "slack:UBOSS")
    s.drain()
    assert sent_count(conn) == 1 and "Sent 2026" in text_of(s.post_for(action))


def test_reject_in_slack_cancels_for_good(conn, ro_conn, pack, clock, test_db_url):
    s = slack_rig(conn, ro_conn, pack, clock, test_db_url)
    conn.execute("UPDATE system_settings SET value = '\"approval\"' WHERE key = 'autonomy_mode'")
    action = s.hot_first_touch()
    s.drain()
    s.press("reject", action, user="UBOSS")
    s.drain(300)
    assert status(conn, action) == ("cancelled", "rejected", "slack:UBOSS") and sent_count(conn) == 0
    assert "Rejected by <@UBOSS>" in text_of(s.post_for(action))


def test_a_user_outside_the_allowed_list_is_refused_and_audited(conn, ro_conn, pack, clock, test_db_url):
    s = slack_rig(conn, ro_conn, pack, clock, test_db_url, allowed=("UALICE",))
    action = s.hot_first_touch()
    s.drain()
    response, _ = s.press("cancel", action, user="UMALLORY")
    assert response.status_code == 200 and status(conn, action)[0] == "pending"
    assert s.slack.ephemerals[-1][1] == "UMALLORY" and "not allowed" in s.slack.ephemerals[-1][2]
    (row,) = audit(conn, "slack.refused_user")
    assert row["action_id"] == str(action)
    ok, _ = s.press("cancel", action, user="UALICE")
    assert status(conn, action)[0] == "cancelled"


def test_an_empty_allowed_list_means_anyone_in_the_channel(s):
    action = s.hot_first_touch()
    s.press("cancel", action, user="USOMEONE")
    assert status(s.conn, action)[2] == "slack:USOMEONE"


def test_a_press_for_an_unknown_action_and_junk_payloads_are_harmless(s):
    import uuid
    response, _ = s.press("cancel", uuid.uuid4())
    assert response.status_code == 200 and audit(s.conn, "slack.unknown")
    import time
    from urllib.parse import urlencode
    for body in (b"payload=notjson", urlencode({"payload": '{"type": "view_submission"}'}).encode(), b"x"):
        ts = str(int(time.time()))
        r = s.client.post("/slack/interactions", content=body, headers={
            "X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sign(body, ts)})
        assert r.status_code == 200


def test_the_button_value_is_only_an_action_id_never_a_decision(s):
    s.hot_first_touch()
    s.drain()
    (post,) = s.slack.posts
    for button in s.slack.buttons(post.ts):
        import uuid
        uuid.UUID(button["value"])                           # nothing but an id


# --- Slack outages and the digest -------------------------------------------------------------------------------------

def test_a_slack_outage_never_blocks_or_repeats_the_send(s):
    from revos.domain.errors import RevosError
    s.slack.fail_next = RevosError("slack down")
    action = s.hot_first_touch()
    s.drain()
    s.drain(61)
    assert sent_count(s.conn) == 1 and status(s.conn, action)[0] == "sent"
    s.drain(3600)
    s.drain(3600)
    assert len(s.slack.posts) == 1                                     # posted once, after the outage


def test_the_daily_digest_posts_a_summary_once_per_day(s):
    s.hot_first_touch()
    s.drain(61)
    assert digest(s.conn, s.clock, s.notifier) is True
    assert digest(s.conn, s.clock, s.notifier) is False
    digests = [p for p in s.slack.posts if "digest" in p.text]
    assert len(digests) == 1
    body = text_of(digests[0])
    for label in ("Leads by stage", "Emails sent", "Replies", "Vetoes", "Held sends", "Deferred sends",
                  "Dead letters", "New lessons", "Deals stalled"):
        assert label in body
    assert len(audit(s.conn, "tracking.digest")) == 1
    s.clock.advance(86400)
    assert digest(s.conn, s.clock, s.notifier) is True and len([p for p in s.slack.posts if "digest" in p.text]) == 2


def test_the_digest_job_posts_through_the_worker_and_survives_a_slack_failure(s):
    from revos.domain.errors import RevosError
    s.slack.fail_next = RevosError("down")
    record_event(s.conn, "digest:test", "tracking.digest", {}, s.clock)
    s.drain()
    s.drain(3600)
    s.drain(3600)
    assert len([p for p in s.slack.posts if "digest" in p.text]) == 1
    assert len(audit(s.conn, "tracking.digest")) == 1


def test_syncing_an_unchanged_action_again_changes_nothing(s):
    action = s.hot_first_touch()
    s.drain()
    assert s.notifier.sync(action) == "unchanged" and s.slack.updates == []
    s.drain(61)
    updates = len(s.slack.updates)
    assert s.notifier.sync(action) == "unchanged" and len(s.slack.updates) == updates
    assert len(s.slack.posts) == 1


def test_a_request_signed_with_an_empty_secret_is_refused_when_no_secret_is_configured():
    import hashlib
    import hmac
    import time

    from fastapi.testclient import TestClient

    from revos.api.main import create_app
    raw = b"payload=%7B%7D"
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(b"", b"v0:" + ts.encode() + b":" + raw, hashlib.sha256).hexdigest()
    response = TestClient(create_app(None, "")).post("/slack/interactions", content=raw, headers={
        "X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig})
    assert response.status_code == 401


def test_the_veto_window_follows_a_deliberate_override_and_otherwise_the_packs_own(conn, ro_conn, pack, clock,
                                                                                    test_db_url):
    for override, expected in ((None, 60), (180, 180)):
        conn.execute("TRUNCATE actions, deals, contacts, companies, events, jobs, audit_log, "
                     "slack_messages, slack_interactions CASCADE")
        s = slack_rig(conn, ro_conn, pack, clock, test_db_url)
        s.rig.orch.veto_window_seconds = override
        action = s.hot_first_touch()
        window = conn.execute("SELECT veto_until - %s FROM actions WHERE id = %s", (clock.now(), action)
                              ).fetchone()[0]
        assert window.total_seconds() == expected


def test_a_send_that_fails_after_the_window_released_it_is_retried_and_sent_once(s):
    from revos.adapters.mock.mailer import MockMailer
    from revos.domain.errors import RevosError

    class Down(MockMailer):
        failing = True

        def perform(self, action):
            if self.failing:
                raise RevosError("mail is down")
            return super().perform(action)

    mailer = Down()
    s.rig.orch.provider._routes["first_touch"] = mailer
    action = s.hot_first_touch()
    s.drain(61)                                    # the window ends, the release succeeds, the send fails
    assert status(s.conn, action)[0] == "approved" and sent_count(s.conn) == 0
    mailer.failing = False
    s.drain(3600)                                  # the job's retry
    s.drain(3600)
    assert status(s.conn, action)[0] == "sent" and sent_count(s.conn) == 1
    assert len(mailer.sent) == 1
