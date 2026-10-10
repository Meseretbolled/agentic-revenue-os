import pytest
from typer.testing import CliRunner

from revos.adapters.hubspot.client import HubSpotClient
from revos.adapters.hubspot.crm import HubSpotCRM
from revos.adapters.hubspot.schema import PROPERTIES
from revos.cli import main as cli
from tests.fakes.hubspot import TOKEN, FakeHubSpot


@pytest.fixture
def fake(monkeypatch, test_db_url):
    fake = FakeHubSpot()
    client = HubSpotClient(TOKEN, http=fake.client(), sleep=lambda s: None)
    monkeypatch.setattr(cli, "_hubspot_client", lambda: client)
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    return fake, client


def test_hubspot_setup_reports_counts_and_is_quiet_the_second_time(fake) -> None:
    fake_hubspot, _ = fake
    first = CliRunner().invoke(cli.app, ["hubspot-setup"])
    total = sum(len(v) for v in PROPERTIES.values())
    assert first.exit_code == 0 and f"created {total} propert(ies), 0 already there" in first.output
    second = CliRunner().invoke(cli.app, ["hubspot-setup"])
    assert "created 0 propert(ies)" in second.output and f"{total} already there" in second.output
    assert TOKEN not in first.output + second.output and fake_hubspot.properties["deals"]


def test_hubspot_setup_without_a_token_says_so_and_does_nothing(monkeypatch) -> None:
    monkeypatch.delenv("HUBSPOT_TOKEN", raising=False)
    result = CliRunner().invoke(cli.app, ["hubspot-setup"])
    assert result.exit_code == 2 and "HUBSPOT_TOKEN is not set" in result.output


def test_a_forbidden_response_is_a_clean_message_and_exit_1(fake) -> None:
    fake_hubspot, _ = fake
    fake_hubspot.fail_next.append(403)
    result = CliRunner().invoke(cli.app, ["hubspot-setup"])
    assert result.exit_code == 1 and "403 forbidden" in result.output and TOKEN not in result.output


def test_reset_demo_needs_the_hubspot_flag(fake) -> None:
    result = CliRunner().invoke(cli.app, ["reset-demo"])
    assert result.exit_code == 2 and "nothing selected" in result.output


def test_reset_demo_asks_first_and_deletes_only_flagged_records(fake) -> None:
    fake_hubspot, client = fake
    crm = HubSpotCRM(client)
    crm.upsert_contact("demo@x.example", {}, "k1")
    fake_hubspot.objects["contacts"]["keep"] = {"id": "keep", "properties": {"email": "real@customer.example"}}
    declined = CliRunner().invoke(cli.app, ["reset-demo", "--hubspot"], input="n\n")
    assert declined.exit_code == 1 and len(fake_hubspot.objects["contacts"]) == 2
    dry = CliRunner().invoke(cli.app, ["reset-demo", "--hubspot", "--dry-run"])
    assert "1 contacts" in dry.output and len(fake_hubspot.objects["contacts"]) == 2
    done = CliRunner().invoke(cli.app, ["reset-demo", "--hubspot", "--yes"])
    assert done.exit_code == 0 and "deleted: 0 deals, 1 contacts" in done.output
    assert set(fake_hubspot.objects["contacts"]) == {"keep"}


def test_reset_demo_with_nothing_flagged_does_nothing(fake) -> None:
    result = CliRunner().invoke(cli.app, ["reset-demo", "--hubspot", "--yes"])
    assert result.exit_code == 0 and "0 deals, 0 contacts" in result.output and "deleted" not in result.output


def test_the_crm_is_the_mock_without_a_token_and_hubspot_with_one(monkeypatch) -> None:
    from revos.adapters.mock.crm import MockCRM
    from revos.composition import build_crm
    monkeypatch.delenv("HUBSPOT_TOKEN", raising=False)
    assert isinstance(build_crm(), MockCRM)
    monkeypatch.setenv("HUBSPOT_TOKEN", "placeholder-not-a-real-token")
    assert isinstance(build_crm(), HubSpotCRM)
