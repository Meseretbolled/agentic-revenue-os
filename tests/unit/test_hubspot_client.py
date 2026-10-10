import httpx
import pytest

from revos.adapters.hubspot.client import (
    HubSpotClient,
    HubSpotError,
    HubSpotUnavailable,
    category_for,
)
from revos.domain.errors import PermanentError
from tests.fakes.hubspot import TOKEN, FakeHubSpot


def make(fake: FakeHubSpot | None = None, **kw):
    fake = fake or FakeHubSpot()
    slept: list[float] = []
    client = HubSpotClient(TOKEN, "https://api.hubapi.com", http=fake.client(), sleep=slept.append, **kw)
    return client, fake, slept


def test_the_token_is_sent_as_a_bearer_token_and_never_shown() -> None:
    client, fake, _ = make()
    client.request("GET", "/crm/v3/objects/contacts", params={"limit": 1})
    assert fake.headers_seen[0]["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in repr(client) and TOKEN not in str(vars(client).get("base_url"))


def test_a_429_is_retried_after_the_time_hubspot_asks_for() -> None:
    client, fake, slept = make()
    fake.fail_next.extend([429, 429])
    fake.retry_after = "3"
    client.request("GET", "/crm/v3/objects/contacts")
    assert slept == [3.0, 3.0] and client.requests_made == 3


def test_server_errors_are_retried_with_growing_jittered_waits_then_reported_as_unavailable() -> None:
    client, fake, slept = make(max_retries=4)
    fake.fail_next.extend([500, 502, 503, 504, 500, 500])
    with pytest.raises(HubSpotUnavailable) as err:
        client.request("GET", "/crm/v3/objects/contacts")
    assert client.requests_made == 5 and len(slept) == 4
    assert slept[0] < slept[2] and all(b >= 0.25 for b in slept) and err.value.category == "server_error"


def test_a_transient_error_that_clears_succeeds() -> None:
    client, fake, slept = make()
    fake.fail_next.extend([503])
    assert client.request("GET", "/crm/v3/objects/contacts")["results"] == []
    assert len(slept) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_permanent_errors_are_not_retried(status) -> None:
    client, fake, slept = make()
    fake.fail_next.append(status)
    with pytest.raises(HubSpotError) as err:
        client.request("GET", "/crm/v3/objects/contacts")
    assert client.requests_made == 1 and slept == [] and err.value.status == status
    assert isinstance(err.value, PermanentError) and err.value.category == category_for(status)


def test_errors_carry_status_and_category_but_never_a_body_or_the_token() -> None:
    client, fake, _ = make()
    fake.fail_next.append(403)
    with pytest.raises(HubSpotError) as err:
        client.request("POST", "/crm/v3/objects/contacts", json={"properties": {"email": "secret@x.example"}})
    text = str(err.value)
    assert "403" in text and "forbidden" in text
    assert TOKEN not in text and "secret@x.example" not in text and "Bearer" not in text


@pytest.mark.parametrize("exc,category", [(httpx.ReadTimeout("slow"), "timeout"),
                                          (httpx.ConnectError("down"), "network")])
def test_timeouts_and_dropped_connections_are_retried_then_reported(exc, category) -> None:
    calls = []

    def handler(request):
        calls.append(1)
        raise exc

    client = HubSpotClient(TOKEN, http=httpx.Client(transport=httpx.MockTransport(handler)),
                           sleep=lambda s: None, max_retries=2)
    with pytest.raises(HubSpotUnavailable) as err:
        client.request("GET", "/crm/v3/objects/contacts")
    assert len(calls) == 3 and err.value.category == category


def test_a_create_is_not_blindly_retried_after_an_ambiguous_failure() -> None:
    """A timeout or 5xx on a create may mean HubSpot DID create it, so the client must not repeat it."""
    client, fake, slept = make()
    fake.fail_next.append(502)
    with pytest.raises(HubSpotUnavailable):
        client.request("POST", "/crm/v3/objects/contacts", json={"properties": {}}, retry_ambiguous=False)
    assert client.requests_made == 1 and slept == []
    fake.fail_next.append(429)                                   # a 429 was never processed: safe to repeat
    client.request("POST", "/crm/v3/objects/contacts", json={"properties": {"email": "a@x.example"}},
                   retry_ambiguous=False)
    assert client.requests_made == 3


def test_a_204_returns_nothing() -> None:
    client, fake, _ = make()
    fake.objects["contacts"]["1"] = {"id": "1", "properties": {}}
    assert client.request("DELETE", "/crm/v3/objects/contacts/1") is None


def test_search_follows_the_paging_cursor_to_the_end() -> None:
    client, fake, _ = make()
    fake.page_size = 2
    for i in range(5):
        fake.objects["contacts"][str(i)] = {"id": str(i), "properties": {"revos_demo": "true"}}
    fake.objects["contacts"]["x"] = {"id": "x", "properties": {"revos_demo": "false"}}
    found = list(client.search_all("contacts", [{"propertyName": "revos_demo", "operator": "EQ",
                                                 "value": "true"}], ["revos_demo"]))
    assert [r["id"] for r in found] == ["0", "1", "2", "3", "4"]
    assert sum(1 for m, p in fake.calls if p.endswith("/search")) == 3


def test_listing_also_pages() -> None:
    client, fake, _ = make()
    fake.page_size = 3
    for i in range(7):
        fake.objects["deals"][str(i)] = {"id": str(i), "properties": {}}
    assert len(list(client.list_all("deals", ["dealname"]))) == 7


def test_paging_stops_at_the_page_limit_instead_of_looping_forever() -> None:
    client, fake, _ = make()
    fake.page_size = 1
    for i in range(10):
        fake.objects["deals"][str(i)] = {"id": str(i), "properties": {}}
    assert len(list(client.list_all("deals", [], page_size=1, max_pages=4))) == 4


def test_the_waits_are_jittered_not_a_fixed_number() -> None:
    import random
    waits = []
    for seed in (1, 2, 3):
        client, fake, slept = make(rng=random.Random(seed), max_retries=1)
        fake.fail_next.extend([500, 500])
        with pytest.raises(HubSpotUnavailable):
            client.request("GET", "/crm/v3/objects/contacts")
        waits.append(slept[0])
    assert len(set(waits)) == 3 and all(0.25 <= w <= 0.5 for w in waits)


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("slow"), httpx.ReadError("dropped")])
def test_a_create_is_not_repeated_after_a_timeout_or_dropped_connection(exc) -> None:
    calls = []

    def handler(request):
        calls.append(1)
        raise exc

    client = HubSpotClient(TOKEN, http=httpx.Client(transport=httpx.MockTransport(handler)),
                           sleep=lambda s: None, max_retries=4)
    with pytest.raises(HubSpotUnavailable):
        client.request("POST", "/crm/v3/objects/contacts", json={"properties": {}}, retry_ambiguous=False)
    assert len(calls) == 1                               # HubSpot may have saved it: look, do not repeat
