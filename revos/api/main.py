"""The HTTP API: a health check, the integration status, and the Slack interactions endpoint.

`create_app` takes its collaborators as arguments (so tests can pass a mock Slack and a test
database); the module-level `app` builds them from the environment when the server starts."""

import os
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request, Response

from revos.esign.webhook import HELLO, SignatureWebhook, parse_callback_body
from revos.modes import integration_modes
from revos.obs import log
from revos.slack.interactions import BadSignature, SlackInteractions, parse_press, verify_signature

# What the public tunnel may reach: the two signed webhooks and the bare health check. Everything else
# (status, docs, the OpenAPI schema, any future route) answers 404 unless the request is local.
PUBLIC_ROUTES = frozenset({("POST", "/slack/interactions"), ("POST", "/dropboxsign/webhook"),
                           ("GET", "/healthz")})
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})


def _via_tunnel(request: Request) -> bool:
    """Fail closed: anything that is not plainly a local request counts as public. The tunnel sends a
    non-local Host and Cloudflare headers, so a request through it is never mistaken for a local one."""
    host = request.headers.get("host", "")
    name = host if host.startswith("[") else host.rsplit(":", 1)[0]
    name = name.split("]")[0] + "]" if host.startswith("[") else name
    return name not in _LOCAL_HOSTS or "cf-ray" in request.headers or "cf-connecting-ip" in request.headers


def create_app(interactions: SlackInteractions | None = None,
               signing_secret: Callable[[], str] | str = "",
               signatures: SignatureWebhook | None = None) -> FastAPI:
    app = FastAPI(title="Agentic Revenue OS", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.interactions = interactions
    app.state.signatures = signatures

    def secret() -> str:
        return signing_secret() if callable(signing_secret) else signing_secret

    @app.middleware("http")
    async def public_allowlist(request: Request, call_next):
        if _via_tunnel(request) and (request.method, request.url.path) not in PUBLIC_ROUTES:
            return Response(status_code=404)
        return await call_next(request)

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        """One log line per request to the Slack endpoint: that it arrived and how it was answered.
        Never the body, the headers or the query string."""
        response = await call_next(request)
        if request.url.path == "/slack/interactions":
            log("slack", "interaction request", method=request.method, status=response.status_code,
                content_length=request.headers.get("content-length"))
        return response

    @app.get("/health")
    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/status")
    def status() -> dict[str, list[dict[str, str]]]:
        """Which integrations run as REAL and which as MOCK. Never a key or a value."""
        return {"integrations": [{"service": m.service, "mode": m.mode, "why": m.why}
                                 for m in integration_modes()]}

    @app.post("/slack/interactions")
    async def slack_interactions(request: Request) -> Response:
        raw = await request.body()                       # the raw bytes: the signature covers exactly these
        try:
            verify_signature(secret(), raw, request.headers.get("X-Slack-Request-Timestamp"),
                             request.headers.get("X-Slack-Signature"))
        except BadSignature as exc:
            log("slack", "interaction rejected", level="warning", reason=str(exc))     # never the body
            return Response(status_code=401)
        press = parse_press(raw)
        service: SlackInteractions | None = request.app.state.interactions
        if press is None or service is None:
            return Response(status_code=200)             # not a button we handle: acknowledge, do nothing
        try:
            service.handle(press)
        except Exception as exc:  # noqa: BLE001
            log("slack", "interaction failed", level="error", error=type(exc).__name__)
            return Response(status_code=500)             # Slack may retry; the press was not recorded
        return Response(status_code=200)

    @app.post("/dropboxsign/webhook")
    async def dropboxsign_webhook(request: Request) -> Response:
        service: SignatureWebhook | None = request.app.state.signatures
        if service is None:
            return Response(status_code=404)
        form = await request.form()
        payload = parse_callback_body({k: v for k, v in form.items() if isinstance(v, str)})
        if payload is None:
            log("esign", "webhook rejected", level="warning", why="no readable json field")
            return Response(status_code=401)
        try:
            outcome = service.handle(payload)
        except Exception as exc:  # noqa: BLE001
            log("esign", "webhook failed", level="error", error=type(exc).__name__)
            return Response(status_code=500)            # the event was not recorded: Dropbox Sign resends it
        if outcome == "rejected":
            return Response(status_code=401)
        return Response(content=HELLO, status_code=200, media_type="text/plain")

    return app


def build_default_app() -> FastAPI:
    """The app `revos run` serves: real Slack when its token is set, the mock otherwise."""
    from revos.adapters.clock import SystemClock
    from revos.composition import build_esign, build_slack
    from revos.db import connect
    allowed = [u for u in os.environ.get("SLACK_ALLOWED_USERS", "").split(",") if u.strip()]
    slack, _ = build_slack()
    interactions = SlackInteractions(connect, SystemClock(), slack, allowed)
    signatures = SignatureWebhook(build_esign(), connect, SystemClock())
    return create_app(interactions, lambda: os.environ.get("SLACK_SIGNING_SECRET", ""), signatures)


class _LazyApp:
    """`uvicorn revos.api.main:app` and the supervisor import `app`; build it on first use."""

    def __init__(self) -> None:
        self._app: Any = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if self._app is None:
            self._app = build_default_app()
        await self._app(scope, receive, send)


app = _LazyApp()
