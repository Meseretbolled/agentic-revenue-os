"""Real by default when the keys exist; mock only when they are missing; never a quiet fallback."""

import httpx
import pytest
from typer.testing import CliRunner

from revos import composition, doctor
from revos.adapters.gmail.mailer import GmailMailer
from revos.adapters.hubspot.client import HubSpotClient, HubSpotUnavailable
from revos.adapters.hubspot.crm import HubSpotCRM
from revos.adapters.mock.crm import MockCRM
from revos.adapters.mock.mailer import MockMailer
from revos.cli.main import app
from revos.doctor import FAILED, Check
from revos.modes import MOCK, REAL, integration_modes, mode_of

KEYS = ("HUBSPOT_TOKEN", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN",
        "FIREFLIES_API_KEY", "DROPBOX_SIGN_API_KEY", "TAVILY_API_KEY", "POSTAL_ADDRESS", "LLM_MODE")


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    for name in KEYS:
        monkeypatch.delenv(name, raising=False)


def test_with_no_keys_everything_but_postgres_is_a_mock() -> None:
    modes = {m.service: m.mode for m in integration_modes({})}
    assert modes.pop("Postgres") == REAL and set(modes.values()) == {MOCK}


def test_each_key_switches_its_own_service_to_real() -> None:
    env = {"HUBSPOT_TOKEN": "t", "GMAIL_ADDRESS": "a@gmail.com", "GMAIL_APP_PASSWORD": "p",
           "LLM_MODE": "live", "ANTHROPIC_API_KEY": "k"}
    modes = {m.service: m.mode for m in integration_modes(env)}
    assert modes["HubSpot"] == modes["Gmail"] == modes["LLM budget"] == REAL
    assert {m.service: m.mode for m in integration_modes({"GMAIL_ADDRESS": "a@gmail.com"})}["Gmail"] == MOCK


def test_a_key_for_a_service_without_an_adapter_yet_is_honestly_a_mock() -> None:
    (slack,) = [m for m in integration_modes({"SLACK_BOT_TOKEN": "x"}) if m.service == "Slack"]
    assert slack.mode == MOCK and "key found" in slack.why and "adapter arrives" in slack.why


def test_the_composition_root_agrees_with_the_modes(monkeypatch) -> None:
    assert isinstance(composition.build_crm(), MockCRM)
    monkeypatch.setenv("HUBSPOT_TOKEN", "a-token")
    assert mode_of("HubSpot") == REAL and isinstance(composition.build_crm(), HubSpotCRM)
    monkeypatch.setenv("GMAIL_ADDRESS", "owner@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password-1234")
    monkeypatch.setenv("POSTAL_ADDRESS", "1 Real Road")
    mailer, _ = composition.build_mail(None)
    assert mode_of("Gmail") == REAL and isinstance(mailer, GmailMailer) and not isinstance(mailer, MockMailer)


def test_keys_with_a_missing_requirement_fail_loudly_not_to_a_mock(monkeypatch) -> None:
    monkeypatch.setenv("GMAIL_ADDRESS", "owner@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password-1234")
    with pytest.raises(ValueError, match="POSTAL_ADDRESS"):
        composition.build_mail(None)
    monkeypatch.setenv("LLM_MODE", "live")
    with pytest.raises(ValueError, match="refusing to fall back"):
        composition.build_runner(None)


def test_a_key_that_exists_but_whose_service_fails_fails_doctor_and_is_not_replaced_by_a_mock(monkeypatch) -> None:
    monkeypatch.setenv("HUBSPOT_TOKEN", "a-token")
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503, json={})))
    assert doctor.probe_hubspot({"HUBSPOT_TOKEN": "a-token"}, down).status == FAILED
    crm = composition.build_crm()
    assert isinstance(crm, HubSpotCRM) and not isinstance(crm, MockCRM)
    crm._c = HubSpotClient("a-token", http=down, sleep=lambda s: None)          # the same outage
    with pytest.raises(HubSpotUnavailable):
        crm.find_by_key("anything")                        # an error, never a quiet empty answer


def test_doctor_prints_what_each_service_will_run_as(monkeypatch) -> None:
    monkeypatch.setenv("HUBSPOT_TOKEN", "a-token")
    monkeypatch.setattr(doctor, "run_checks", lambda: [Check("HubSpot", FAILED, "auth"),
                                                       Check("Slack", "MOCK", "key missing")])
    result = CliRunner().invoke(app, ["doctor"])
    lines = {line.split()[0]: line for line in result.output.splitlines()}
    assert "will run as: REAL" in lines["HubSpot"] and "REAL FAILED" in lines["HubSpot"]
    assert "will run as: MOCK" in lines["Slack"] and result.exit_code == 1


def test_run_refuses_to_start_when_a_real_service_fails_and_never_swaps_in_a_mock(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HUBSPOT_TOKEN", "a-token")
    monkeypatch.setenv("REVOS_LOG_FILE", str(tmp_path / "log.jsonl"))
    monkeypatch.setattr(doctor, "run_checks", lambda: [Check("HubSpot", FAILED, "auth")])
    started = []
    import revos.runtime.supervisor as sup
    monkeypatch.setattr(sup, "Supervisor", lambda **kw: started.append(kw))
    result = CliRunner().invoke(app, ["run", "--no-api"])
    assert result.exit_code == 1 and not started
    assert "never swaps in a mock" in result.output and "HubSpot" in result.output
    log = (tmp_path / "log.jsonl").read_text()
    assert "will run as REAL but its check failed" in log and "a-token" not in log


def test_a_failing_check_for_a_service_that_runs_as_a_mock_does_not_block_start_and_the_banner_lists_modes(
        monkeypatch, tmp_path, test_db_url) -> None:
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-key")
    monkeypatch.setenv("REVOS_LOG_FILE", str(tmp_path / "log.jsonl"))
    monkeypatch.setattr(doctor, "run_checks", lambda: [Check("Tavily", FAILED, "key rejected")])

    class Stub:
        def __init__(self, **kw):
            import threading
            self.stop_event = threading.Event()
            self.errors = []

        def start(self):
            self.stop_event.set()

        def stop(self):
            from revos.runtime.supervisor import ShutdownReport
            return ShutdownReport()

    import revos.runtime.supervisor as sup
    monkeypatch.setattr(sup, "Supervisor", Stub)
    result = CliRunner().invoke(app, ["run", "--no-api"])
    assert result.exit_code == 0, result.output
    assert "HubSpot      MOCK" in result.output or "HubSpot       MOCK" in result.output
    assert "Tavily" in result.output and "stopped cleanly" in result.output
