"""The dashboard: every page renders, sign-in and CSRF are enforced, the public tunnel never reaches it, its
actions go through the system's own paths, and its controls really change what the policy engine does."""

import json
import re
from datetime import timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from revos.api.main import create_app
from revos.dashboard.auth import COOKIE, PRE_COOKIE, Auth
from revos.dashboard.router import DashboardConfig
from revos.orchestrator.dashboard_demo import clear_demo, has_demo, seed_demo
from revos.policy.settings import autonomy_mode, kill_switch_on
from revos.seed import load_profiles
from tests.conftest import PACK_ID
from tests.pipeline_helpers import audit, make_rig

TOKEN = "correct-horse-battery-staple-for-tests"
LOCAL = "http://localhost"
TUNNEL = "https://some-name.trycloudflare.com"

PAGES = ["/dashboard/", "/dashboard/pending", "/dashboard/activity", "/dashboard/queue", "/dashboard/lessons",
         "/dashboard/integrations", "/dashboard/controls"]


@pytest.fixture
def cfg(test_db_url, clock):
    return DashboardConfig(Auth(TOKEN), lambda: psycopg.connect(test_db_url), clock,
                           {"LLM_MAX_SPEND_USD": "3.00", "HUBSPOT_TOKEN": "hs-secret-value-123"})


@pytest.fixture
def app(cfg):
    return create_app(None, "", None, dashboard=cfg)


@pytest.fixture
def client(app):
    return TestClient(app, base_url=LOCAL, follow_redirects=False)


def csrf_in(html: str) -> str:
    return re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)


def sign_in(client: TestClient, token: str = TOKEN):
    page = client.get("/dashboard/login")
    return client.post("/dashboard/login", data={"token": token, "csrf": csrf_in(page.text)})


@pytest.fixture
def signed(client):
    assert sign_in(client).status_code == 303
    assert COOKIE in client.cookies
    return client


@pytest.fixture
def demo(conn, clock, pack):
    assert seed_demo(conn, clock, "test_seg", pack.rate_card) == 7
    return conn


def deal_id(conn, company: str) -> str:
    return str(conn.execute("SELECT d.id FROM deals d JOIN companies c ON c.id = d.company_id WHERE c.name LIKE %s",
                            (company + "%",)).fetchone()[0])


def token_for(client) -> str:
    return csrf_in(client.get("/dashboard/controls").text)


# --- the pages --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders_when_signed_in(signed, demo, path):
    response = signed.get(path)
    assert response.status_code == 200 and "Revenue OS" in response.text
    assert response.headers["cache-control"] == "no-store" and "default-src 'none'" in response.headers[
        "content-security-policy"]


def test_the_board_has_a_card_per_deal_in_its_stage_column(signed, demo):
    html = signed.get("/dashboard/").text
    for company in ("Northwind Optical (demo)", "Lakeside Dental Group (demo)", "Harbor Auto Care (demo)"):
        assert company in html
    assert "demo</span>" in html and 'href="/dashboard/deals/' in html
    assert "Owner: outreach" in html and "in the veto window" in html and "needs approval" in html


def test_the_deal_page_shows_score_io_call_quotes_lessons_pending_and_the_timeline(signed, demo):
    html = signed.get(f"/dashboard/deals/{deal_id(demo, 'Harbor Auto Care')}").text
    assert "Operates 96 locations" in html and "+30" in html                       # each rule's written reason
    assert 'chip auto">auto</span>' in html and "Package: <strong>scale" in html or "scale" in html
    assert "Monthly subtotal" in html and "$40,000.00" in html and "$120,000.00" in html
    assert "Our budget is about $40,000 a month across all locations." in html   # the verbatim quote
    assert "found in transcript" in html                                         # re-checked against the transcript
    assert "L1" in html and "CPM objection" in html                              # lessons cited
    assert "stage new -&gt; enriched" in html or "stage new -> enriched" in html or "enriched" in html
    assert "Timeline" in html and "Emails" in html and "received" in html


def test_a_deal_that_does_not_exist_or_is_not_an_id_is_a_404(signed, demo):
    assert signed.get("/dashboard/deals/00000000-0000-0000-0000-000000000000").status_code == 404
    assert signed.get("/dashboard/deals/not-an-id").status_code == 404


def test_the_activity_feed_filters_by_actor_and_type(signed, demo):
    everything = signed.get("/dashboard/activity").text
    assert "demo-seed" in everything and "deal.stage_changed" in everything
    only = signed.get("/dashboard/activity?actor=demo-seed&event=policy.decided").text
    assert "policy.decided" in only and "deal.stage_changed</td>" not in only


def test_pending_shows_the_veto_countdown_approvals_and_blocked_items_without_buttons(signed, demo):
    html = signed.get("/dashboard/pending").text
    assert "Cancel send" in html and 'class="countdown big"' in html
    assert "Approve" in html and "Reject" in html
    blocked = html.split("Blocked by policy")[1]
    assert "discount_over_maximum" in blocked and "<button" not in blocked      # no button on a hard block


def test_a_superseded_write_cancelled_by_policy_is_not_listed_as_a_hard_block(signed, demo):
    deal = deal_id(demo, "Harbor Auto Care")
    demo.execute("INSERT INTO actions (idempotency_key, deal_id, type, autonomy_level, status, decision, decision_by, "
                 "decided_at, payload) VALUES ('x:superseded', %s, 'crm_update', 'auto', 'cancelled', 'rejected', "
                 "'policy', now(), '{\"subject\": \"stale write\"}')", (deal,))
    html = signed.get("/dashboard/pending").text
    assert "stale write" not in html and "discount_over_maximum" in html


def test_queue_shows_depth_oldest_job_and_dead_letters_with_a_retry_button(signed, demo):
    html = signed.get("/dashboard/queue").text
    assert "dead letters" in html and "demo dead letter" in html and "Retry" in html and "oldest waiting job" in html


def test_lessons_show_evidence_and_the_drafts_that_cited_them(signed, demo):
    html = signed.get("/dashboard/lessons").text
    assert "Local video CPMs have been climbing." in html and "Harbor Auto Care (demo)" in html and "follow up" in html


def test_integrations_show_real_or_mock_and_model_spend_never_a_key(signed, demo):
    html = signed.get("/dashboard/integrations").text
    for service in ("HubSpot", "Gmail", "Slack", "Fireflies", "Dropbox Sign", "LLM budget", "Tunnel URL"):
        assert service in html
    assert "REAL" in html and "of <strong>$3.00</strong>" in html and "LLM_MAX_SPEND_USD" in html
    assert "hs-secret-value-123" not in html


def test_the_controls_page_shows_each_value_and_where_it_comes_from(signed, demo):
    html = signed.get("/dashboard/controls").text
    assert "Kill switch" in html and "Autonomy mode" in html and "Veto window" in html
    assert "Effective from: database" in html and "Effective from: default (pack / built-in)" in html


def test_the_integrations_page_marks_the_probe_as_checking_until_it_has_answered(cfg, conn):
    from revos.dashboard.router import HealthProbe
    cfg.health = HealthProbe(lambda: [])
    client = TestClient(create_app(None, "", None, dashboard=cfg), base_url=LOCAL, follow_redirects=False)
    sign_in(client)
    assert "Checking the services now" in client.get("/dashboard/integrations").text


# --- sign-in -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", PAGES + ["/dashboard/deals/00000000-0000-0000-0000-000000000000"])
def test_every_page_sends_a_visitor_without_a_session_to_the_login_page(client, path):
    response = client.get(path)
    assert response.status_code == 303 and response.headers["location"] == "/dashboard/login"


def test_a_forged_or_expired_session_cookie_is_not_a_session(client, cfg):
    client.cookies.set(COOKIE, "9999999999.abcd.deadbeef", path="/dashboard")
    assert client.get("/dashboard/").status_code == 303
    expired = Auth(TOKEN, now=lambda: 1_000.0).new_session()
    client.cookies.set(COOKIE, expired, path="/dashboard")
    assert client.get("/dashboard/").status_code == 303                     # signed correctly, but expired
    other = Auth("a-different-token").new_session()
    client.cookies.set(COOKIE, other, path="/dashboard")
    assert client.get("/dashboard/").status_code == 303                     # signed with another token's key


def test_a_wrong_token_is_refused_and_the_right_one_sets_an_httponly_strict_cookie(client):
    assert sign_in(client, "not-the-token").headers["location"] == "/dashboard/login?error=1"
    assert COOKIE not in client.cookies
    response = sign_in(client)
    assert response.status_code == 303 and response.headers["location"] == "/dashboard/"
    cookie = next(c for c in response.headers.get_list("set-cookie") if c.startswith(COOKIE))
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/dashboard" in cookie
    assert TOKEN not in cookie


def test_the_login_form_needs_its_own_csrf_token(client):
    client.get("/dashboard/login")
    assert client.post("/dashboard/login", data={"token": TOKEN}).status_code == 403
    assert client.post("/dashboard/login", data={"token": TOKEN, "csrf": "0" * 64}).status_code == 403
    assert PRE_COOKIE in client.cookies and COOKIE not in client.cookies


def test_five_wrong_tokens_lock_the_client_out_even_for_the_right_one(client):
    for _ in range(5):
        sign_in(client, "wrong")
    assert sign_in(client).status_code == 429


def test_with_no_token_configured_every_page_is_off(test_db_url, clock):
    cfg = DashboardConfig(Auth(""), lambda: psycopg.connect(test_db_url), clock)
    client = TestClient(create_app(None, "", None, dashboard=cfg), base_url=LOCAL, follow_redirects=False)
    for path in ("/dashboard/", "/dashboard/login", "/dashboard/controls"):
        assert client.get(path).status_code == 503
    assert client.post("/dashboard/controls/kill", data={"on": "1", "csrf": "x"}).status_code == 503
    assert Auth("").session_ok(Auth("").new_session()) is False


def test_the_token_never_appears_in_any_page_or_header(signed, demo):
    for path in PAGES:
        response = signed.get(path)
        assert TOKEN not in response.text and TOKEN not in str(response.headers)


def test_the_session_is_forgotten_on_sign_out(signed):
    signed.post("/dashboard/logout", data={"csrf": token_for(signed)})
    assert signed.get("/dashboard/").status_code == 303


# --- CSRF ------------------------------------------------------------------------------------------------------

def one(conn, sql):
    return conn.execute(sql).fetchone()[0]


POSTS = [("/dashboard/controls/kill", {"on": "1"}), ("/dashboard/controls/mode", {"mode": "approval"}),
         ("/dashboard/controls/veto", {"seconds": "30"}), ("/dashboard/logout", {})]


@pytest.mark.parametrize("path,form", POSTS)
def test_a_post_without_or_with_a_wrong_csrf_token_is_forbidden_and_changes_nothing(signed, conn, path, form):
    assert signed.post(path, data=form).status_code == 403
    assert signed.post(path, data={**form, "csrf": "f" * 64}).status_code == 403
    assert not kill_switch_on(conn) and autonomy_mode(conn).value == "autonomous"
    assert conn.execute("SELECT 1 FROM system_settings WHERE key = 'veto_window_seconds'").fetchone() is None
    assert audit(conn, "system.kill_switch") == [] and audit(conn, "system.veto_window_seconds") == []
    assert signed.get("/dashboard/").status_code == 200                      # still signed in


def test_every_action_post_needs_csrf_too(signed, demo):
    action = one(demo, "SELECT id FROM actions WHERE status = 'pending' AND veto_until IS NOT NULL")
    dead = one(demo, "SELECT id FROM dead_letters")
    for path in (f"/dashboard/actions/{action}/cancel", f"/dashboard/actions/{action}/approve",
                 f"/dashboard/actions/{action}/reject", f"/dashboard/queue/{dead}/retry"):
        assert signed.post(path).status_code == 403
    assert one(demo, f"SELECT status FROM actions WHERE id = '{action}'") == "pending"
    assert one(demo, "SELECT retried_at IS NULL FROM dead_letters") is True


def test_a_csrf_token_from_another_session_does_not_work(app, signed):
    other = TestClient(app, base_url=LOCAL, follow_redirects=False)
    sign_in(other)
    assert signed.post("/dashboard/controls/kill", data={"on": "1", "csrf": token_for(other)}).status_code == 403


# --- the public tunnel never reaches it -----------------------------------------------------------------------

def test_every_dashboard_route_is_refused_through_the_public_tunnel_even_with_a_valid_session(app, signed, demo):
    cookie = signed.cookies.get(COOKIE)
    tunnel = TestClient(app, base_url=TUNNEL, follow_redirects=False)
    headers = {"cookie": f"{COOKIE}={cookie}"}
    action = one(demo, "SELECT id FROM actions WHERE status = 'pending' AND veto_until IS NOT NULL")
    for path in [*PAGES, "/dashboard/login", "/dashboard/static/dashboard.css", "/dashboard/deals/x"]:
        assert tunnel.get(path, headers=headers).status_code == 404, path
    for path in ("/dashboard/login", "/dashboard/logout", "/dashboard/controls/kill",
                 f"/dashboard/actions/{action}/cancel"):
        assert tunnel.post(path, data={"csrf": csrf_in(signed.get("/dashboard/controls").text), "on": "1"},
                           headers=headers).status_code == 404, path
    assert not kill_switch_on(demo) and one(demo, f"SELECT status FROM actions WHERE id = '{action}'") == "pending"


def test_a_cloudflare_header_on_a_local_host_name_is_still_the_tunnel(signed):
    for header in ("cf-ray", "cf-connecting-ip"):
        assert signed.get("/dashboard/", headers={header: "x"}).status_code == 404


def test_the_dashboard_is_reachable_locally_by_every_loopback_name(app):
    for base in ("http://localhost", "http://127.0.0.1:8100", "http://[::1]:8100"):
        assert TestClient(app, base_url=base, follow_redirects=False).get("/dashboard/login").status_code == 200


# --- masking, escaping, no inline script ------------------------------------------------------------------------

def test_email_addresses_are_masked_everywhere(signed, demo):
    demo.execute("UPDATE messages SET body = body || 'Write to jane.doe+tag@gmail.com or ops@acme.example' "
                 "WHERE direction = 'inbound'")
    html = signed.get(f"/dashboard/deals/{deal_id(demo, 'Harbor Auto Care')}").text
    assert "jane.doe" not in html and "ops@acme" not in html and "j***@gmail.com" in html
    assert "demo.contact" not in signed.get("/dashboard/activity").text


def test_text_from_outside_is_escaped_not_run(signed, demo):
    demo.execute("UPDATE messages SET body = '<script>alert(1)</script><img src=x onerror=alert(2)>' "
                 "WHERE direction = 'inbound'")
    html = signed.get(f"/dashboard/deals/{deal_id(demo, 'Harbor Auto Care')}").text
    assert "<script>alert" not in html and "&lt;script&gt;alert(1)" in html and "<img src=x" not in html


def test_the_templates_hold_no_inline_script_style_or_event_handler():
    from pathlib import Path
    for path in Path("revos/dashboard/templates").glob("*.html"):
        text = path.read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)", text), path
        assert ' style="' not in text and not re.search(r"\son\w+=", text), path


# --- the actions go through the system's own paths ----------------------------------------------------------------

def test_cancel_stops_a_send_in_its_window_and_is_audited(signed, demo):
    action = one(demo, "SELECT id FROM actions WHERE status = 'pending' AND veto_until IS NOT NULL")
    response = signed.post(f"/dashboard/actions/{action}/cancel", data={"csrf": token_for(signed)})
    assert response.status_code == 303 and "ok=1" in response.headers["location"]
    row = demo.execute("SELECT status, decision, decision_by FROM actions WHERE id = %s", (action,)).fetchone()
    assert row == ("cancelled", "rejected" if False else row[1], "dashboard") and row[1] == "vetoed"
    assert len(audit(demo, "dashboard.cancelled")) == 1 and len(audit(demo, "action.vetoed")) == 1
    again = signed.post(f"/dashboard/actions/{action}/cancel", data={"csrf": token_for(signed)})
    assert "ok=0" in again.headers["location"]                                    # a second cancel is refused


def test_cancel_after_the_window_has_closed_is_refused(signed, demo, clock):
    action = one(demo, "SELECT id FROM actions WHERE status = 'pending' AND veto_until IS NOT NULL")
    clock.advance(3600)
    response = signed.post(f"/dashboard/actions/{action}/cancel", data={"csrf": token_for(signed)})
    assert "ok=0" in response.headers["location"]
    assert one(demo, f"SELECT status FROM actions WHERE id = '{action}'") == "pending"


def test_approve_records_the_decision_and_queues_the_send_for_the_outbox(signed, demo):
    action = one(demo, "SELECT id FROM actions WHERE status = 'awaiting_approval'")
    signed.post(f"/dashboard/actions/{action}/approve", data={"csrf": token_for(signed)})
    assert demo.execute("SELECT status, decision, decision_by FROM actions WHERE id = %s", (action,)
                        ).fetchone() == ("approved", "approved", "dashboard")
    assert demo.execute("SELECT count(*) FROM events WHERE idempotency_key = %s",
                        (f"approved-send:{action}",)).fetchone()[0] == 1     # the send step re-reads the approval
    assert demo.execute("SELECT count(*) FROM approvals WHERE action_id = %s", (action,)).fetchone()[0] == 1


def test_reject_cancels_and_nothing_is_queued(signed, demo):
    action = one(demo, "SELECT id FROM actions WHERE status = 'awaiting_approval'")
    signed.post(f"/dashboard/actions/{action}/reject", data={"csrf": token_for(signed)})
    assert one(demo, f"SELECT status FROM actions WHERE id = '{action}'") == "cancelled"
    assert demo.execute("SELECT count(*) FROM events WHERE idempotency_key LIKE 'approved-send:%%'"
                        ).fetchone()[0] == 0


def test_a_hard_blocked_action_cannot_be_approved_or_rejected_even_by_a_crafted_post(signed, demo):
    blocked = one(demo, "SELECT id FROM actions WHERE status = 'cancelled' AND decision_by = 'policy'")
    for verb in ("approve", "reject", "cancel"):
        response = signed.post(f"/dashboard/actions/{blocked}/{verb}", data={"csrf": token_for(signed)})
        assert "ok=0" in response.headers["location"]
    assert demo.execute("SELECT status, decision_by FROM actions WHERE id = %s", (blocked,)).fetchone() == (
        "cancelled", "policy")
    assert demo.execute("SELECT count(*) FROM events WHERE idempotency_key LIKE 'approved-send:%%'"
                        ).fetchone()[0] == 0


def test_retrying_a_dead_letter_requeues_the_job_and_is_audited_with_the_actor(signed, demo):
    dead = one(demo, "SELECT id FROM dead_letters")
    signed.post(f"/dashboard/queue/{dead}/retry", data={"csrf": token_for(signed)})
    assert one(demo, "SELECT status FROM jobs WHERE id = (SELECT job_id FROM dead_letters)") == "queued"
    (row,) = audit(demo, "job.retried")
    assert row["dead_letter_id"] == dead
    assert demo.execute("SELECT actor FROM audit_log WHERE event = 'job.retried'").fetchone()[0] == "dashboard"
    again = signed.post(f"/dashboard/queue/{dead}/retry", data={"csrf": token_for(signed)})
    assert "ok=0" in again.headers["location"]                                    # already retried


def test_malformed_ids_and_numbers_are_refused_not_crashes(signed, demo):
    csrf = token_for(signed)
    assert "ok=0" in signed.post("/dashboard/actions/not-a-uuid/cancel", data={"csrf": csrf}).headers["location"]
    assert "ok=0" in signed.post("/dashboard/queue/abc/retry", data={"csrf": csrf}).headers["location"]
    assert "ok=0" in signed.post("/dashboard/controls/veto", data={"csrf": csrf, "seconds": "abc"}).headers["location"]


def test_the_dashboard_never_writes_a_deal_stage(signed, demo):
    before = demo.execute("SELECT id, stage FROM deals ORDER BY id").fetchall()
    csrf = token_for(signed)
    for action in demo.execute("SELECT id FROM actions").fetchall():
        for verb in ("approve", "reject", "cancel"):
            signed.post(f"/dashboard/actions/{action[0]}/{verb}", data={"csrf": csrf})
    assert demo.execute("SELECT id, stage FROM deals ORDER BY id").fetchall() == before


# --- the controls really change the policy ---------------------------------------------------------------------------

def hot_rig(conn, ro_conn, pack, clock, **lead):
    profile = load_profiles(PACK_ID)["hot"]
    return make_rig(conn, ro_conn, pack, clock, domain=profile["domain"], company=profile["company"],
                    name=profile["contact"], fields=json.dumps(profile["fields"]), **lead)


def first_touch(conn, ro_conn, pack, clock):
    rig = hot_rig(conn, ro_conn, pack, clock)
    return rig, rig.orch.handle_lead_created(rig.deal_id)


def test_the_veto_window_set_in_the_dashboard_decides_how_long_a_new_send_waits(
        signed, conn, ro_conn, pack, clock):
    signed.post("/dashboard/controls/veto", data={"csrf": token_for(signed), "seconds": "137"})
    assert "Effective from: database" in signed.get("/dashboard/controls").text
    rig, first = first_touch(conn, ro_conn, pack, clock)
    until = one(conn, f"SELECT veto_until FROM actions WHERE id = '{first.action_id}'")
    assert until == clock.now() + timedelta(seconds=137)
    (row,) = audit(conn, "system.veto_window_seconds")
    assert row["value"] == 137 and conn.execute(
        "SELECT actor FROM audit_log WHERE event = 'system.veto_window_seconds'").fetchone()[0] == "dashboard"


def test_clearing_the_veto_override_hands_control_back_to_the_environment(signed, conn):
    signed.post("/dashboard/controls/veto", data={"csrf": token_for(signed), "seconds": "90"})
    signed.post("/dashboard/controls/veto", data={"csrf": token_for(signed), "seconds": ""})
    assert conn.execute("SELECT 1 FROM system_settings WHERE key = 'veto_window_seconds'").fetchone() is None
    assert "default (pack / built-in)" in signed.get("/dashboard/controls").text


@pytest.mark.parametrize("seconds", ["0", "4", "3601", "-5", "1.5", "99999999999"])
def test_a_veto_window_outside_5_to_3600_seconds_is_refused(signed, conn, seconds):
    response = signed.post("/dashboard/controls/veto", data={"csrf": token_for(signed), "seconds": seconds})
    assert "ok=0" in response.headers["location"]
    assert conn.execute("SELECT 1 FROM system_settings WHERE key = 'veto_window_seconds'").fetchone() is None


def test_the_environment_value_applies_until_the_dashboard_overrides_it(conn):
    from revos.policy.settings import effective_controls, set_veto_window
    env = {"VETO_WINDOW_SECONDS": "45"}
    got = effective_controls(conn, env, 60)["veto_window_seconds"]
    assert (got.value, got.source) == (45, "environment (VETO_WINDOW_SECONDS)")
    set_veto_window(conn, 200, "dashboard")
    got = effective_controls(conn, env, 60)["veto_window_seconds"]
    assert (got.value, got.source, got.updated_by) == (200, "database", "dashboard")


def test_a_junk_value_in_the_settings_table_is_ignored_not_trusted(conn):
    from revos.policy.settings import veto_window_override
    for junk in ("0", "99999", '"soon"', "true", "null"):
        conn.execute("INSERT INTO system_settings (key, value) VALUES ('veto_window_seconds', %s::jsonb) "
                     "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (junk,))
        assert veto_window_override(conn) is None


def test_approval_mode_set_in_the_dashboard_makes_a_new_send_wait_for_a_person(
        signed, conn, ro_conn, pack, clock):
    rig, first = first_touch(conn, ro_conn, pack, clock)
    assert one(conn, f"SELECT status FROM actions WHERE id = '{first.action_id}'") in ("pending", "approved")
    signed.post("/dashboard/controls/mode", data={"csrf": token_for(signed), "mode": "approval"})
    assert autonomy_mode(conn).value == "approval"
    second = hot_rig(conn, ro_conn, pack, clock, email="second.lead@brightside-optical.example")
    result = second.orch.handle_lead_created(second.deal_id)
    assert one(conn, f"SELECT status FROM actions WHERE id = '{result.action_id}'") == "awaiting_approval"
    assert conn.execute("SELECT actor FROM audit_log WHERE event = 'system.autonomy_mode'").fetchone()[0] == "dashboard"
    signed.post("/dashboard/controls/mode", data={"csrf": token_for(signed), "mode": "chaos"})
    assert autonomy_mode(conn).value == "approval"                                # an unknown mode is refused


def test_the_kill_switch_set_in_the_dashboard_stops_a_send_and_turning_it_off_releases_it(
        signed, conn, ro_conn, pack, clock):
    rig, first = first_touch(conn, ro_conn, pack, clock)
    clock.advance(3600)
    signed.post("/dashboard/controls/kill", data={"csrf": token_for(signed), "on": "1"})
    assert kill_switch_on(conn) and "KILL SWITCH ON" in signed.get("/dashboard/").text
    assert rig.orch.send_action(first.action_id).status == "wait"
    assert rig.world.mailer.sent == []
    assert conn.execute("SELECT actor FROM audit_log WHERE event = 'system.kill_switch'").fetchone()[0] == "dashboard"
    signed.post("/dashboard/controls/kill", data={"csrf": token_for(signed), "on": "0"})
    assert not kill_switch_on(conn)
    assert [r["value"] for r in audit(conn, "system.kill_switch")] == [True, False]   # both changes on record


# --- the demo data ---------------------------------------------------------------------------------------------------

def test_seeding_is_marked_idempotent_and_sends_nothing(conn, clock, pack):
    assert seed_demo(conn, clock, "test_seg", pack.rate_card) == 7 and seed_demo(conn, clock, "test_seg") == 0
    assert has_demo(conn)
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0          # nothing queued
    assert conn.execute("SELECT count(*) FROM jobs WHERE status = 'queued'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM contacts WHERE email NOT LIKE '%%.invalid'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM companies WHERE name NOT LIKE '%%(demo)'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM deals WHERE fields->>'demo' IS DISTINCT FROM 'true'").fetchone()[0] == 0
    assert clear_demo(conn) == 7 and not has_demo(conn)
    assert conn.execute("SELECT count(*) FROM audit_log WHERE actor = 'demo-seed'").fetchone()[0] > 0   # append-only
