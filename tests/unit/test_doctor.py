import imaplib
import os

import httpx
import pytest
from typer.testing import CliRunner

from revos import doctor
from revos.cli.main import app
from revos.doctor import FAILED, MOCK, OK, Check, probe_hubspot, run_checks
from revos.env import load_env_file

SECRETS = {"HUBSPOT_TOKEN": "pat-eu1-SECRET-HUB", "SLACK_BOT_TOKEN": "xoxb-SECRET-SLACK",
           "GMAIL_ADDRESS": "owner@example.test", "GMAIL_APP_PASSWORD": "SECRETGMAILPASS1234",
           "FIREFLIES_API_KEY": "SECRET-FIRE", "DROPBOX_SIGN_API_KEY": "SECRET-DSIGN",
           "TAVILY_API_KEY": "tvly-SECRET", "PUBLIC_BASE_URL": "https://demo.example.test",
           "PROSPECT_TEST_EMAIL": "prospect@example.test"}


class DB:
    def __init__(self, ok=True):
        self.ok = ok

    def __call__(self):
        if not self.ok:
            import psycopg
            raise psycopg.OperationalError("connection refused")
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, *a):
        return self


def http_with(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok_everywhere(request):
    if "hubapi" in request.url.host:
        return httpx.Response(200, json={"results": []})
    if request.url.host == "slack.com":
        return httpx.Response(200, json={"ok": True})
    if "fireflies" in request.url.host:
        return httpx.Response(200, json={"data": {"user": {"user_id": "u"}}})
    return httpx.Response(200, json={})


def by_service(checks):
    return {c.service: c for c in checks}


# --- one status per service, never a value ---------------------------------------------------------------

def test_with_no_keys_every_service_but_postgres_is_a_mock() -> None:
    checks = run_checks({}, http=http_with(ok_everywhere), connect=DB())
    got = by_service(checks)
    assert got["Postgres"].status == OK
    assert all(c.status == MOCK for name, c in got.items() if name != "Postgres")
    assert [c.service for c in checks] == ["Postgres", "LLM budget", "HubSpot", "Slack", "Gmail",
                                          "Fireflies", "Dropbox Sign", "Tavily", "Tunnel URL"]
    assert all(c.status in {OK, FAILED, MOCK} for c in checks)


def test_with_working_keys_every_service_is_real_ok() -> None:
    checks = run_checks({**SECRETS, "DROPBOX_SIGN_TEST_MODE": "true"}, http=http_with(ok_everywhere),
                        connect=DB(), login=lambda a, p: None)
    assert {c.service: c.status for c in checks if c.service != "LLM budget"} == {
        s: OK for s in ("Postgres", "HubSpot", "Slack", "Gmail", "Fireflies", "Dropbox Sign",
                        "Tavily", "Tunnel URL")}


def test_nothing_secret_is_ever_printed() -> None:
    def failing(request):
        return httpx.Response(401, json={"error": "invalid", "echo": request.headers.get("authorization")})

    def bad_login(address, password):
        raise imaplib.IMAP4.error(f"login failed for {address} {password}")

    checks = run_checks({**SECRETS, "DROPBOX_SIGN_TEST_MODE": "true"}, http=http_with(failing),
                        connect=DB(), login=bad_login)
    text = "\n".join(c.line() for c in checks)
    for value in SECRETS.values():
        assert value not in text, value
    assert "Bearer" not in text and "pat-" not in text
    assert all(c.line().count("REAL") + c.line().count("MOCK") == 1 for c in checks)   # exactly one status


# --- HubSpot: does the key work as a Bearer token, and on which base? -----------------------------------------

def hubspot_env(base=None):
    env = {"HUBSPOT_TOKEN": SECRETS["HUBSPOT_TOKEN"]}
    if base:
        env["HUBSPOT_BASE_URL"] = base
    return env


def test_hubspot_is_probed_with_a_bearer_token_on_the_contacts_endpoint() -> None:
    seen = []

    def handler(request):
        seen.append((request.method, str(request.url), request.headers["authorization"]))
        return httpx.Response(200, json={"results": []})

    check = probe_hubspot(hubspot_env(), http_with(handler))
    assert check.status == OK and check.detail == ""
    assert seen == [("GET", "https://api.hubapi.com/crm/v3/objects/contacts?limit=1",
                     f"Bearer {SECRETS['HUBSPOT_TOKEN']}")]


@pytest.mark.parametrize("first_status", [401, 403, 404])
def test_a_failure_on_the_configured_base_tries_the_other_eu_base(first_status) -> None:
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(first_status if request.url.host == "api.hubapi.com" else 200, json={})

    check = probe_hubspot(hubspot_env(), http_with(handler))
    assert hosts == ["api.hubapi.com", "api-eu1.hubapi.com"]
    assert check.status == OK and "HUBSPOT_BASE_URL=https://api-eu1.hubapi.com" in check.detail


def test_the_alternative_works_the_other_way_round_too() -> None:
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(401 if request.url.host == "api-eu1.hubapi.com" else 200, json={})

    check = probe_hubspot(hubspot_env("https://api-eu1.hubapi.com"), http_with(handler))
    assert hosts == ["api-eu1.hubapi.com", "api.hubapi.com"]
    assert check.status == OK and "HUBSPOT_BASE_URL=https://api.hubapi.com" in check.detail


def test_when_both_bases_refuse_only_the_status_and_category_are_reported() -> None:
    check = probe_hubspot(hubspot_env(), http_with(lambda r: httpx.Response(401, json={"message": "no"})))
    assert check == Check("HubSpot", FAILED, "auth, status 401")


@pytest.mark.parametrize("status,category", [(403, "forbidden"), (404, "not_found")])
def test_other_refusals_are_categorised(status, category) -> None:
    check = probe_hubspot(hubspot_env(), http_with(lambda r: httpx.Response(status, json={})))
    assert check.status == FAILED and check.detail == f"{category}, status {status}"


def test_a_server_error_does_not_trigger_the_alternative_base() -> None:
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(503, json={})

    check = probe_hubspot(hubspot_env(), http_with(handler))
    assert hosts == ["api.hubapi.com"] and check.detail == "server_error, status 503"


@pytest.mark.parametrize("exc,category", [(httpx.ReadTimeout("t"), "timeout"), (httpx.ConnectError("c"), "network")])
def test_hubspot_network_failures_are_categorised(exc, category) -> None:
    def handler(request):
        raise exc

    assert probe_hubspot(hubspot_env(), http_with(handler)) == Check("HubSpot", FAILED, category)


# --- the other services ---------------------------------------------------------------------------------------------

def test_slack_failures_are_categorised() -> None:
    env = {"SLACK_BOT_TOKEN": "xoxb-x"}
    bad = http_with(lambda r: httpx.Response(200, json={"ok": False, "error": "invalid_auth"}))
    other = http_with(lambda r: httpx.Response(200, json={"ok": False, "error": "ratelimited"}))
    assert doctor.check_slack(env, bad) == Check("Slack", FAILED, "auth")
    assert doctor.check_slack(env, other) == Check("Slack", FAILED, "api_error")


@pytest.mark.parametrize("exc,category", [(imaplib.IMAP4.error("bad"), "auth"), (TimeoutError(), "timeout"),
                                          (OSError("no route"), "network")])
def test_gmail_failures_are_categorised(exc, category) -> None:
    def login(address, password):
        raise exc

    got = doctor.check_gmail({"GMAIL_ADDRESS": "a@x.test", "GMAIL_APP_PASSWORD": "p"}, login)
    assert got == Check("Gmail", FAILED, category)


def test_gmail_needs_both_values_and_notes_a_missing_prospect_inbox() -> None:
    assert doctor.check_gmail({"GMAIL_ADDRESS": "a@x.test"}).status == MOCK
    got = doctor.check_gmail({"GMAIL_ADDRESS": "a@x.test", "GMAIL_APP_PASSWORD": "p"}, lambda a, p: None)
    assert got.status == OK and "PROSPECT_TEST_EMAIL" in got.detail


def test_dropbox_sign_refuses_to_pass_with_test_mode_off() -> None:
    env = {"DROPBOX_SIGN_API_KEY": "k", "DROPBOX_SIGN_TEST_MODE": "false"}
    got = doctor.check_dropbox_sign(env, http_with(ok_everywhere))
    assert got.status == FAILED and "test mode" in got.detail


def test_tavily_plan_limits_and_the_tunnel() -> None:
    assert doctor.check_tavily({"TAVILY_API_KEY": "k"}, http_with(lambda r: httpx.Response(432, json={}))
                               ) == Check("Tavily", FAILED, "plan_limit")
    assert doctor.check_tunnel({"PUBLIC_BASE_URL": "https://t.example.test"},
                               http_with(lambda r: httpx.Response(530, json={}))).status == FAILED
    assert doctor.check_tunnel({}, http_with(ok_everywhere)) == Check("Tunnel URL", MOCK, "no public URL set")


def test_postgres_down_is_a_real_failure() -> None:
    assert doctor.check_postgres({}, DB(ok=False)) == Check("Postgres", FAILED, "network")


def test_the_llm_budget_is_a_mock_unless_live_with_a_key() -> None:
    assert doctor.check_llm({}, DB()).status == MOCK
    assert doctor.check_llm({"LLM_MODE": "live"}, DB()) == Check("LLM budget", MOCK, "key missing")


# --- the command and the .env loader ----------------------------------------------------------------------------------

def test_the_command_prints_one_line_per_service_and_exits_1_on_a_failure(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "run_checks", lambda: [Check("HubSpot", FAILED, "auth, status 401"),
                                                       Check("Slack", MOCK, "key missing")])
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert result.output.splitlines() == ["HubSpot       REAL FAILED  (auth, status 401)",
                                          "Slack         MOCK  (key missing)"]
    monkeypatch.setattr(doctor, "run_checks", lambda: [Check("Slack", OK)])
    assert CliRunner().invoke(app, ["doctor"]).exit_code == 0


def test_the_env_loader_fills_gaps_never_overrides_and_reports_only_a_count(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("REVOS_NO_DOTENV")
    for name in ("A_NEW", "B_QUOTED", "C_SET", "D_EMPTY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("C_SET", "from the shell")
    path = tmp_path / ".env"
    path.write_text("# a comment\nA_NEW=1  # trailing note\nB_QUOTED=\"two words\"\nC_SET=from the file\n"
                    "D_EMPTY=\nnot a line\n\n")
    assert load_env_file(path) == 2
    assert os.environ["A_NEW"] == "1" and os.environ["B_QUOTED"] == "two words"
    assert os.environ["C_SET"] == "from the shell" and "D_EMPTY" not in os.environ
    for name in ("A_NEW", "B_QUOTED"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("REVOS_NO_DOTENV", "1")
    assert load_env_file(path) == 0 and "A_NEW" not in os.environ


def test_a_missing_env_file_is_fine(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("REVOS_NO_DOTENV")
    assert load_env_file(tmp_path / "nope") == 0


def test_the_test_suite_never_reads_the_real_env_file() -> None:
    assert os.environ["REVOS_NO_DOTENV"] == "1"
