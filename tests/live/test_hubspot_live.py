"""LIVE: talks to the real HubSpot with the credentials in .env. Skipped unless run with -m live
(`make live-hubspot`). It creates and updates ONE fictional contact, company and deal, then deletes
exactly what it created. Run `revos hubspot-setup` first (the make target does)."""

import os
import time
import uuid

import pytest

from revos.adapters.hubspot.client import DEFAULT_BASE_URL, HubSpotClient
from revos.adapters.hubspot.crm import HubSpotCRM
from revos.doctor import OK, probe_hubspot
from revos.domain.stages import DealStage
from revos.env import load_env_file

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def client() -> HubSpotClient:
    os.environ.pop("REVOS_NO_DOTENV", None)          # the only place the tests read the real .env
    load_env_file()
    token = os.environ.get("HUBSPOT_TOKEN", "").strip()
    if not token:
        pytest.skip("HUBSPOT_TOKEN is not set")
    hub = HubSpotClient(token, os.environ.get("HUBSPOT_BASE_URL", "").strip() or DEFAULT_BASE_URL)
    names = {p["name"] for p in hub.request("GET", "/crm/v3/properties/deals")["results"]}
    if "revos_deal_id" not in names:
        pytest.skip("run `revos hubspot-setup` first")
    return hub


def eventually(check, seconds: float = 20.0):
    """HubSpot's search index lags writes by a few seconds."""
    deadline = time.time() + seconds
    while True:
        value = check()
        if value or time.time() > deadline:
            return value
        time.sleep(1.5)


def test_the_service_key_works_as_a_bearer_token(client) -> None:
    check = probe_hubspot(dict(os.environ), client._http)
    assert check.status == OK, check.line()


def test_one_fictional_contact_company_and_deal_round_trip_and_clean_up(client) -> None:
    crm = HubSpotCRM(client)
    tag = uuid.uuid4().hex[:10]
    deal_uuid = str(uuid.uuid4())
    created: list[tuple[str, str]] = []
    try:
        contact, was_created = crm.upsert_contact(f"revos-live-{tag}@example.com",
                                                  {"firstname": "Livetest", "lastname": "Fictional"}, f"live-c-{tag}")
        created.append(("contacts", contact))
        company, _ = crm.upsert_company(f"revos-live-{tag}.example", {"name": f"Revos Live Test {tag}"},
                                        f"live-co-{tag}")
        created.append(("companies", company))
        deal, deal_created = crm.upsert_deal(deal_uuid, {"dealname": f"Revos live test {tag}"}, f"live-d-{tag}",
                                             stage=DealStage.QUALIFIED, contact_id=contact, company_id=company)
        created.append(("deals", deal))
        assert was_created and deal_created

        got = crm.get_record("deals", deal)
        assert got["revos_demo"] == "true" and got["revos_stage"] == "qualified"
        assert got["dealstage"] == "qualifiedtobuy" and got["revos_deal_id"] == deal_uuid

        again, created_again = eventually(lambda: crm.upsert_deal(
            deal_uuid, {}, f"live-d2-{tag}", stage=DealStage.MEETING) if crm.deal_id_for(deal_uuid) else None)
        assert again == deal and not created_again                         # updated, not duplicated
        assert crm.get_record("deals", deal)["revos_stage"] == "meeting"
        assert crm.get_record("deals", deal)["dealstage"] == "presentationscheduled"

        note = crm.add_note_to("deals", deal, "Call summary from the live test.", f"live-n-{tag}")
        created.append(("notes", note))
        assert eventually(lambda: crm.add_note_to("deals", deal, "again", f"live-n-{tag}") == note)
    finally:
        for kind, record_id in reversed(created):
            client.request("DELETE", f"/crm/v3/objects/{kind}/{record_id}", what=f"cleanup {kind}")
    for kind, record_id in created:
        assert crm.get_record(kind, record_id) is None if kind != "notes" else True
