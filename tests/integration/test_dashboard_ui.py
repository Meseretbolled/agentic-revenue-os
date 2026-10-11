# ruff: noqa: F811
"""The dashboard's look and its newer pages: overview, plain-English activity, the deal journey, acknowledging dead
letters, superseded blocks, honest integration states, and the design rules (no external assets, no inline code)."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from revos.dashboard import words
from revos.dashboard.router import integration_state
from revos.modes import Mode
from tests.integration.test_dashboard import (  # noqa: F401  (fixtures)
    app,
    cfg,
    client,
    csrf_in,
    deal_id,
    demo,
    one,
    sign_in,
    signed,
    token_for,
)
from tests.pipeline_helpers import audit

STATIC = Path("revos/dashboard/static")
TEMPLATES = Path("revos/dashboard/templates")


# --- overview --------------------------------------------------------------------------------------------------

def test_the_overview_with_nothing_to_do_says_so(signed, conn):
    html = signed.get("/dashboard/").text
    assert "Nothing needs you." in html and "Live deals" in html and "Model spend" in html
    assert "Sends in veto window" in html and "System vs human" in html


def test_the_overview_shows_the_kpis_the_funnel_the_hourly_chart_and_what_needs_a_person(signed, demo):
    html = signed.get("/dashboard/").text
    for label in ("Live deals", "Awaiting you", "Sends in veto window", "System vs human", "Model spend"):
        assert label in html
    assert 'class="funnel"' in html and 'class="hours"' in html and html.count('class="svg-bar') >= 24
    assert "Needs you" in html and "Nothing needs you." not in html
    assert "Approve follow up" in html and "dead letter" in html          # an approval and an unacknowledged letter
    assert re.search(r'<span class="kpi-value">\d+</span>\s*<span class="kpi-sub">in an open stage', html)


def test_a_deal_with_no_record_for_three_days_is_listed_as_stalled(signed, conn, demo):
    deal = deal_id(demo, "Harbor Auto Care")
    conn.execute("UPDATE deals SET fields = fields - 'demo' WHERE id = %s", (deal,))     # demo deals are exempt
    conn.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_change")
    try:
        conn.execute("UPDATE audit_log SET at = now() - interval '5 days' WHERE deal_id = %s", (deal,))
    finally:
        conn.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_change")
    html = signed.get("/dashboard/").text
    assert "Stalled:" in html and "Harbor Auto Care (demo)" in html


def test_held_replies_appear_under_needs_you(signed, conn):
    conn.execute("INSERT INTO held_replies (message_id, from_addr, subject, body, received_at, reason, "
                 "candidate_deal_ids) VALUES ('<h1@x>', 'a@b.example', 'Re: hello', 'hi', now(), 'ambiguous', '{}')")
    html = signed.get("/dashboard/").text
    assert "Held reply" in html and "Re: hello" in html and "Nothing needs you." not in html


# --- pipeline -------------------------------------------------------------------------------------------------------

def test_the_pipeline_folds_empty_stages_and_has_no_sideways_scroll(signed, demo):
    html = signed.get("/dashboard/pipeline").text
    assert "Empty stages" in html and 'class="col col-folded"' in html
    css = (STATIC / "dashboard.css").read_text()
    assert re.search(r"\.board\s*\{[^}]*flex-wrap:\s*wrap", css) and "overflow-x: auto; padding-bottom" not in css


def test_the_pipeline_with_no_deals_has_an_empty_state(signed, conn):
    assert "No deals yet." in signed.get("/dashboard/pipeline").text


# --- deal page ---------------------------------------------------------------------------------------------------------

def test_the_deal_page_has_a_stage_stepper_and_an_icon_timeline_with_raw_ids_under_an_expand(signed, demo):
    html = signed.get(f"/dashboard/deals/{deal_id(demo, 'Harbor Auto Care')}").text
    assert 'class="stepper"' in html and 'aria-current="step"' in html and "Out for signature" in html
    assert html.count("step-done") >= 5 and 'class="tl"' in html and 'class="tl-dot"' in html
    assert "<details class=\"raw\"><summary>Details</summary>" in html
    assert re.search(r'class="chip actor-(system|policy|person)"', html)


# --- activity -----------------------------------------------------------------------------------------------------------

def test_activity_reads_as_sentences_with_an_actor_chip_and_raw_ids_under_an_expand(signed, demo):
    html = signed.get("/dashboard/activity").text
    assert "Moved Harbor Auto Care (demo) to Out for signature" in html
    assert 'class="chip actor-system"' in html
    assert "<summary>Details</summary>" in html and "deal.stage_changed" in html     # the raw event name
    assert "deal.stage_changed" not in html.split('<ul class="feed">')[1].split("<details")[0]


def test_activity_filters_still_work(signed, demo):
    only = signed.get("/dashboard/activity?actor=demo-seed&event=policy.decided").text
    assert "Policy blocked" in only and "Moved Harbor" not in only


def test_a_person_acting_from_the_dashboard_gets_the_person_chip(signed, demo):
    action = one(demo, "SELECT id FROM actions WHERE status = 'pending' AND veto_until IS NOT NULL")
    signed.post(f"/dashboard/actions/{action}/cancel", data={"csrf": token_for(signed)})
    html = signed.get("/dashboard/activity").text
    assert "A person cancelled a send from the dashboard" in html and 'class="chip actor-person"' in html


# --- words --------------------------------------------------------------------------------------------------------------

def test_sentences_name_the_people_and_the_company():
    assert words.sentence("action.sent", "outbox", {"type": "first_touch"}, "Harborview", "Maya Ortiz") == \
        "Sent the first touch to Maya Ortiz at Harborview"
    assert words.sentence("deal.stage_changed", "orchestrator", {"to": "closed_won"}, "Harborview") == \
        "Moved Harborview to Closed Won"
    assert words.sentence("job.retried", "dashboard", {"job_id": 7}) == "Retried dead-lettered job 7"
    assert words.sentence("something.new.happened", "x", {}) == "Something new happened"      # never raw JSON


@pytest.mark.parametrize("actor,kind", [("orchestrator", "system"), ("outbox", "system"), ("policy", "policy"),
                                        ("policy:veto_window", "policy"), ("dashboard", "person"),
                                        ("slack:U123", "person"), ("operator", "person"), (None, "system")])
def test_who_did_it_is_system_policy_or_a_person(actor, kind):
    assert words.actor_kind(actor) == kind


def test_relative_time_reads_naturally():
    now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    assert [words.ago(now - timedelta(seconds=s), now) for s in (5, 120, 7200, 3 * 86400)] == [
        "just now", "2m ago", "2h ago", "3d ago"]


# --- queue: acknowledge --------------------------------------------------------------------------------------------------

def dead_ids(conn) -> list[int]:
    return [r[0] for r in conn.execute("SELECT id FROM dead_letters ORDER BY id")]


def add_dead(conn, hours_old: int, error: str = "boom") -> int:
    job = conn.execute("INSERT INTO jobs (type, status, attempts, last_error) VALUES ('x.y', 'dead', 5, %s) "
                       "RETURNING id", (error,)).fetchone()[0]
    return conn.execute("INSERT INTO dead_letters (job_id, type, error, created_at) VALUES (%s, 'x.y', %s, "
                        "now() - make_interval(hours => %s)) RETURNING id", (job, error, hours_old)).fetchone()[0]


def test_acknowledging_a_dead_letter_is_audited_hides_it_from_the_main_list_and_keeps_it_viewable(signed, conn):
    dead = add_dead(conn, 1, "network down")
    assert "network down" in signed.get("/dashboard/queue").text
    response = signed.post(f"/dashboard/queue/{dead}/ack", data={"csrf": token_for(signed)})
    assert "ok=1" in response.headers["location"]
    row = conn.execute("SELECT acknowledged_by, acknowledged_at IS NOT NULL FROM dead_letters").fetchone()
    assert row == ("dashboard", True)
    assert conn.execute("SELECT actor FROM audit_log WHERE event = 'job.acknowledged'").fetchone()[0] == "dashboard"
    html = signed.get("/dashboard/queue").text
    main, acked = html.split("Acknowledged <span")[0], html.split("Acknowledged <span")[1]
    assert "network down" not in main and "network down" in acked
    assert "No unacknowledged dead letters." in html
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "dead"        # nothing was retried or changed


def test_acknowledged_letters_leave_the_overview_count(signed, conn):
    dead = add_dead(conn, 1)
    assert "1 dead letter" in signed.get("/dashboard/").text
    signed.post(f"/dashboard/queue/{dead}/ack", data={"csrf": token_for(signed)})
    html = signed.get("/dashboard/").text
    assert "not acknowledged" not in html and "Nothing needs you." in html


def test_acknowledging_twice_or_after_a_retry_is_refused(signed, conn):
    dead = add_dead(conn, 1)
    signed.post(f"/dashboard/queue/{dead}/ack", data={"csrf": token_for(signed)})
    again = signed.post(f"/dashboard/queue/{dead}/ack", data={"csrf": token_for(signed)})
    assert "ok=0" in again.headers["location"] and len(audit(conn, "job.acknowledged")) == 1
    other = add_dead(conn, 1)
    signed.post(f"/dashboard/queue/{other}/retry", data={"csrf": token_for(signed)})
    late = signed.post(f"/dashboard/queue/{other}/ack", data={"csrf": token_for(signed)})
    assert "ok=0" in late.headers["location"]


def test_acknowledge_all_older_than_n_hours_takes_only_the_older_ones(signed, conn):
    old, recent = add_dead(conn, 30), add_dead(conn, 2)
    response = signed.post("/dashboard/queue/ack-older", data={"csrf": token_for(signed), "hours": "24"})
    assert "ok=1" in response.headers["location"]
    states = dict(conn.execute("SELECT id, acknowledged_at IS NOT NULL FROM dead_letters").fetchall())
    assert states == {old: True, recent: False}
    (row,) = audit(conn, "job.acknowledged_many")
    assert row["dead_letter_ids"] == [old] and row["older_than_hours"] == 24


@pytest.mark.parametrize("hours", ["0", "-3", "abc", "99999", ""])
def test_a_bad_number_of_hours_acknowledges_nothing(signed, conn, hours):
    add_dead(conn, 100)
    signed.post("/dashboard/queue/ack-older", data={"csrf": token_for(signed), "hours": hours})
    assert conn.execute("SELECT count(*) FROM dead_letters WHERE acknowledged_at IS NOT NULL").fetchone()[0] == 0


def test_the_acknowledge_posts_need_csrf(signed, conn):
    dead = add_dead(conn, 50)
    assert signed.post(f"/dashboard/queue/{dead}/ack").status_code == 403
    assert signed.post("/dashboard/queue/ack-older", data={"hours": "1"}).status_code == 403
    assert conn.execute("SELECT count(*) FROM dead_letters WHERE acknowledged_at IS NOT NULL").fetchone()[0] == 0


def test_the_queue_page_shows_real_health(signed, conn):
    add_dead(conn, 1)
    html = signed.get("/dashboard/queue").text
    for label in ("Queued", "Running", "Oldest waiting job", "Dead letters", "not acknowledged"):
        assert label in html


# --- pending: superseded --------------------------------------------------------------------------------------------------

def test_a_block_that_a_later_attempt_replaced_is_superseded_not_a_hard_block(signed, conn, demo):
    deal = deal_id(demo, "Harbor Auto Care")
    blocked = one(demo, "SELECT id FROM actions WHERE status = 'cancelled' AND decision_by = 'policy'")
    html = signed.get("/dashboard/pending").text
    assert "discount_over_maximum" in html.split("Blocked by policy")[1] and "Superseded" not in html
    demo.execute("INSERT INTO actions (idempotency_key, deal_id, type, autonomy_level, status, decision, decision_by, "
                 "decided_at, sent_at, payload, contact_id) VALUES ('retry:1', %s, 'send_io', 'auto', 'sent', "
                 "'approved', 'policy', now(), now(), '{\"subject\": \"IO again\"}', "
                 "(SELECT contact_id FROM deals WHERE id = %s))", (deal, deal))
    html = signed.get("/dashboard/pending").text
    live, history = html.split("Blocked by policy")[1].split("Superseded")[0], html.split("Superseded")[1]
    assert "discount_over_maximum" not in live and "Nothing is blocked right now." in live
    assert "discount_over_maximum" in history and 'class="chip">superseded</span>' in history
    assert "<button" not in history and blocked


# --- integrations: honest states --------------------------------------------------------------------------------------------

def mode(service, kind, built=True, why=""):
    return Mode(service, kind, why, built)


@pytest.mark.parametrize("m,status,answered,expected", [
    (mode("HubSpot", "REAL"), "REAL OK", True, ("REAL OK", "ok")),
    (mode("HubSpot", "REAL"), "REAL FAILED  (auth)", True, ("Needs attention", "bad")),
    (mode("Slack", "MOCK", why="no key"), "", True, ("MOCK", "muted")),
    (mode("Tunnel URL", "REAL"), "REAL FAILED  (network)", True, ("Not running", "warn")),
    (mode("Tunnel URL", "MOCK", why="PUBLIC_BASE_URL is not set"), "", True, ("MOCK", "muted")),
    (mode("Tavily", "MOCK", built=False), "REAL FAILED", True, ("Not used", "muted")),
    (mode("Gmail", "REAL"), "", False, ("Checking", "loading")),
    (mode("Gmail", "REAL"), "", True, ("Not checked", "muted")),
])
def test_each_service_gets_an_honest_state(m, status, answered, expected):
    got = integration_state(m, status, answered)
    assert (got["state"], got["tone"]) == expected


def test_needs_attention_appears_only_for_a_real_service_that_failed(cfg, conn):
    from fastapi.testclient import TestClient

    from revos.api.main import create_app
    from revos.dashboard.router import HealthProbe

    class Check:
        def __init__(self, service, status, detail=""):
            self.service, self.status, self.detail = service, status, detail
    probe = HealthProbe(lambda: [Check("HubSpot", "REAL FAILED", "auth"), Check("Tunnel URL", "REAL FAILED", "network"),
                                 Check("Slack", "REAL OK")])
    cfg.health, cfg.env = probe, {**cfg.env, "PUBLIC_BASE_URL": "https://x.example", "HUBSPOT_TOKEN": "t",
                                  "SLACK_BOT_TOKEN": "s"}
    client = TestClient(create_app(None, "", None, dashboard=cfg), base_url="http://localhost", follow_redirects=False)
    sign_in(client)
    client.get("/dashboard/integrations")
    for _ in range(50):
        if probe.checked_at:
            break
        import time
        time.sleep(0.02)
    html = client.get("/dashboard/integrations").text
    assert html.count("Needs attention") == 1 and "Not running" in html and "Not used" in html
    assert "Last checked" in html


# --- design rules --------------------------------------------------------------------------------------------------------------

def test_the_dashboard_loads_nothing_from_outside_and_ships_no_inline_code_or_style():
    for path in [*TEMPLATES.glob("*.html"), *STATIC.glob("*")]:
        text = path.read_text()
        assert not re.search(r"(src|href)=[\"']https?://", text) and "@import" not in text and "url(http" not in text, path
        if path.suffix == ".html":
            assert not re.search(r"<script(?![^>]*\bsrc=)", text) and ' style="' not in text, path
            assert not re.search(r"\son\w+=", text), path


def test_the_design_system_rules_are_in_the_stylesheet():
    css = (STATIC / "dashboard.css").read_text()
    assert "prefers-color-scheme: light" in css and ":focus-visible" in css and "tabular-nums" in css
    assert "--s1: 8px" in css and "prefers-reduced-motion" in css and ".skeleton" in css
    assert not re.search(r"font-family:[^;]*(googleapis|fonts\.)", css)


def test_the_sidebar_has_an_icon_per_page_and_no_emoji(signed):
    html = signed.get("/dashboard/").text
    assert html.count('<svg class="icon"') >= 9 and 'class="sidebar"' in html and 'aria-current="page"' in html
    assert not re.search(r"[\U0001F300-\U0001FAFF☀-➿]", html)


def test_the_page_header_has_a_live_updated_indicator_and_the_script_keeps_it_current(signed):
    html = signed.get("/dashboard/").text
    assert 'id="updated-text"' in html and 'id="pulse"' in html
    assert "Updated " in (STATIC / "dashboard.js").read_text() and "lastUpdate" in (STATIC / "dashboard.js").read_text()
