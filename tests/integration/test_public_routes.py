"""Only the Slack and Dropbox Sign webhooks (and the bare health check) are reachable through the tunnel."""

import pytest
from fastapi.testclient import TestClient

from revos.api.main import create_app

TUNNEL = "https://some-name.trycloudflare.com"


class RejectingWebhook:
    def handle(self, payload):
        return "rejected"


def client(base: str) -> TestClient:
    return TestClient(create_app(None, "secret", RejectingWebhook()), base_url=base)  # type: ignore[arg-type]


@pytest.mark.parametrize("method,path", [
    ("GET", "/status"), ("GET", "/health"), ("GET", "/"), ("GET", "/docs"), ("GET", "/redoc"),
    ("GET", "/openapi.json"), ("POST", "/status"), ("GET", "/slack/interactions"),
    ("GET", "/dropboxsign/webhook"), ("GET", "/dashboard"),
])
def test_through_the_tunnel_everything_else_is_404(method, path):
    assert client(TUNNEL).request(method, path).status_code == 404


def test_through_the_tunnel_the_webhooks_and_health_answer():
    c = client(TUNNEL)
    assert c.get("/healthz").json() == {"status": "ok"}
    assert c.post("/slack/interactions", content=b"x").status_code == 401         # reaches the signature check
    body = {"json": (None, '{"event": {"event_type": "x"}}')}
    assert c.post("/dropboxsign/webhook", files=body).status_code == 401          # reaches the handler


def test_a_local_host_name_with_a_cloudflare_header_still_counts_as_public():
    c = client("http://localhost")
    assert c.get("/status", headers={"cf-ray": "abc"}).status_code == 404
    assert c.get("/status", headers={"cf-connecting-ip": "1.2.3.4"}).status_code == 404


@pytest.mark.parametrize("base", ["http://localhost", "http://127.0.0.1:8100", "http://[::1]:8100"])
def test_locally_the_status_page_still_answers(base):
    assert client(base).get("/status").status_code == 200
