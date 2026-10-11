"""The HTTP API: a health check, the integration status, and the Slack interactions endpoint.

`create_app` takes its collaborators as arguments (so tests can pass a mock Slack and a test
database); the module-level `app` builds them from the environment when the server starts."""

import os
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request, Response

from revos.modes import integration_modes
from revos.obs import log
from revos.slack.interactions import BadSignature, SlackInteractions, parse_press, verify_signature


def create_app(interactions: SlackInteractions | None = None,
               signing_secret: Callable[[], str] | str = "") -> FastAPI:
    app = FastAPI(title="Agentic Revenue OS")
    app.state.interactions = interactions

    def secret() -> str:
        return signing_secret() if callable(signing_secret) else signing_secret

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

    return app


def build_default_app() -> FastAPI:
    """The app `revos run` serves: real Slack when its token is set, the mock otherwise."""
    from revos.adapters.clock import SystemClock
    from revos.composition import build_slack
    from revos.db import connect
    allowed = [u for u in os.environ.get("SLACK_ALLOWED_USERS", "").split(",") if u.strip()]
    slack, _ = build_slack()
    interactions = SlackInteractions(connect, SystemClock(), slack, allowed)
    return create_app(interactions, lambda: os.environ.get("SLACK_SIGNING_SECRET", ""))


class _LazyApp:
    """`uvicorn revos.api.main:app` and the supervisor import `app`; build it on first use."""

    def __init__(self) -> None:
        self._app: Any = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if self._app is None:
            self._app = build_default_app()
        await self._app(scope, receive, send)


app = _LazyApp()
