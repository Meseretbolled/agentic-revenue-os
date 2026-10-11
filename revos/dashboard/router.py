"""The dashboard's routes. Pages are rendered on the server (Jinja, autoescaped); a few lines of JavaScript
refresh the page every 5 seconds and run the veto countdowns. Everything lives under /dashboard so one rule
can keep it off the public tunnel."""

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from revos.adapters.spend import SpendLedger
from revos.dashboard import actions, data
from revos.dashboard.auth import COOKIE, PRE_COOKIE, SESSION_SECONDS, Auth
from revos.modes import integration_modes
from revos.policy.settings import effective_controls
from revos.ports.clock import Clock

HERE = Path(__file__).parent
BASE = "/dashboard"
HEADERS = {
    "Cache-Control": "no-store", "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": ("default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self'; "
                                "connect-src 'self'; form-action 'self'; base-uri 'none'; "
                                "frame-ancestors 'none'"),
}


class HealthProbe:
    """Live service checks (`revos doctor`'s), run in the background and cached: a page that polls every
    five seconds must never wait on Gmail or Slack. Until the first result the page says "checking"."""

    def __init__(self, run: Callable[[], list[Any]], ttl: float = 60.0) -> None:
        self._run, self._ttl = run, ttl
        self._result: dict[str, tuple[str, str]] = {}
        self._at = 0.0
        self._busy = False
        self._lock = threading.Lock()

    def snapshot(self) -> dict[str, tuple[str, str]]:
        with self._lock:
            stale = time.monotonic() - self._at > self._ttl and not self._busy
            if stale:
                self._busy = True
        if stale:
            threading.Thread(target=self._refresh, daemon=True).start()
        return dict(self._result)

    def _refresh(self) -> None:
        try:
            result = {c.service: (c.status, c.detail or "") for c in self._run()}
        except Exception as exc:  # noqa: BLE001 - a failing probe must not take the page down
            result = {"(health probe)": ("FAILED", type(exc).__name__)}
        with self._lock:
            self._result, self._at, self._busy = result, time.monotonic(), False


@dataclass
class DashboardConfig:
    auth: Auth
    connect: Callable[[], psycopg.Connection]
    clock: Clock
    env: Mapping[str, str] = field(default_factory=dict)
    health: HealthProbe | None = None
    pack_veto_seconds: int = 60


def build_router(cfg: DashboardConfig) -> APIRouter:
    router = APIRouter(prefix=BASE)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["mask"] = data.mask
    templates.env.filters["money"] = lambda v: f"${v:,.2f}" if isinstance(v, int | float) else "-"
    templates.env.filters["when"] = lambda v: v.strftime("%Y-%m-%d %H:%M:%S") if v else "-"

    def reader() -> psycopg.Connection:
        conn = cfg.connect()
        conn.autocommit = True
        return conn

    def signed_in(request: Request) -> str | None:
        cookie = request.cookies.get(COOKIE)
        return cookie if cfg.auth.session_ok(cookie) else None

    def render(request: Request, name: str, cookie: str, active: str, **ctx: Any) -> HTMLResponse:
        with reader() as conn:
            waiting = conn.execute("SELECT count(*) FROM actions WHERE status = 'awaiting_approval' OR "
                                   "(status = 'pending' AND veto_until > now())").fetchone()[0]
            kill = effective_controls(conn, cfg.env, cfg.pack_veto_seconds)["kill_switch"].value
        page = {"active": active, "csrf": cfg.auth.csrf_for(cookie), "waiting": waiting, "kill": kill,
                "now": cfg.clock.now(), "msg": request.query_params.get("msg", "")[:200],
                "msg_ok": request.query_params.get("ok") == "1", **ctx}
        response = templates.TemplateResponse(request, name, page)
        response.headers.update(HEADERS)
        return response

    def disabled() -> Response:
        return PlainTextResponse("The dashboard is off: set DASHBOARD_TOKEN and restart.", status_code=503,
                                 headers=HEADERS)

    def to_login() -> Response:
        return RedirectResponse(f"{BASE}/login", status_code=303, headers=HEADERS)

    def back(path: str, result: actions.Result) -> Response:
        return RedirectResponse(f"{BASE}{path}{'&' if '?' in path else '?'}ok={int(result.ok)}"
                                f"&msg={quote(result.message)}", status_code=303, headers=HEADERS)

    # --- sign in -----------------------------------------------------------------------------------------
    @router.get("/login")
    def login_form(request: Request) -> Response:
        if not cfg.auth.enabled:
            return disabled()
        pre = request.cookies.get(PRE_COOKIE) or cfg.auth.new_pre()
        response = templates.TemplateResponse(request, "login.html", {
            "csrf": cfg.auth.login_csrf(pre), "error": request.query_params.get("error", "")})
        response.headers.update(HEADERS)
        response.set_cookie(PRE_COOKIE, pre, httponly=True, samesite="strict", path=BASE, max_age=900)
        return response

    @router.post("/login")
    async def login(request: Request) -> Response:
        if not cfg.auth.enabled:
            return disabled()
        form = await request.form()
        client = request.client.host if request.client else "?"
        if not cfg.auth.login_csrf_ok(request.cookies.get(PRE_COOKIE), str(form.get("csrf", ""))):
            return PlainTextResponse("Forbidden", status_code=403, headers=HEADERS)
        if cfg.auth.locked_out(client):
            return PlainTextResponse("Too many attempts. Wait a few minutes.", status_code=429,
                                     headers=HEADERS)
        if not cfg.auth.token_ok(str(form.get("token", ""))):
            cfg.auth.note_failure(client)
            return RedirectResponse(f"{BASE}/login?error=1", status_code=303, headers=HEADERS)
        response = RedirectResponse(f"{BASE}/", status_code=303, headers=HEADERS)
        response.set_cookie(COOKIE, cfg.auth.new_session(), httponly=True, samesite="strict", path=BASE,
                            max_age=SESSION_SECONDS)
        response.delete_cookie(PRE_COOKIE, path=BASE)
        return response

    @router.post("/logout")
    async def logout(request: Request) -> Response:
        cookie = request.cookies.get(COOKIE)
        form = await request.form()
        if not cfg.auth.csrf_ok(cookie, str(form.get("csrf", ""))):
            return PlainTextResponse("Forbidden", status_code=403, headers=HEADERS)
        response = to_login()
        response.delete_cookie(COOKIE, path=BASE)
        return response

    # --- pages -------------------------------------------------------------------------------------------
    def page(path: str, template: str, active: str, build: Callable[..., dict[str, Any]]):
        @router.get(path, name=template)
        def handler(request: Request) -> Response:
            if not cfg.auth.enabled:
                return disabled()
            cookie = signed_in(request)
            if cookie is None:
                return to_login()
            with reader() as conn:
                ctx = build(conn, request)
            if ctx is None:
                return PlainTextResponse("Not found", status_code=404, headers=HEADERS)
            return render(request, template, cookie, active, **ctx)
        return handler

    page("/", "board.html", "board", lambda conn, r: {
        "columns": data.board(conn, cfg.clock.now()), "stages": data.BOARD_STAGES})
    page("/activity", "activity.html", "activity", lambda conn, r: {
        "actor": r.query_params.get("actor", ""), "event": r.query_params.get("event", ""),
        **data.activity(conn, r.query_params.get("actor", ""), r.query_params.get("event", ""))})
    page("/pending", "pending.html", "pending", lambda conn, r: data.pending(conn, cfg.clock.now()))
    page("/queue", "queue.html", "queue", lambda conn, r: {"q": data.queue(conn, cfg.clock.now())})
    page("/lessons", "lessons.html", "lessons", lambda conn, r: {"lessons": data.lessons(conn)})

    def deal_ctx(conn: psycopg.Connection, request: Request) -> dict[str, Any] | None:
        try:
            deal_id = str(UUID(request.path_params["deal_id"]))
        except ValueError:
            return None
        found = data.deal(conn, deal_id, cfg.clock.now())
        return {"d": found} if found else None
    page("/deals/{deal_id}", "deal.html", "board", deal_ctx)

    def integrations_ctx(conn: psycopg.Connection, request: Request) -> dict[str, Any]:
        live = cfg.health.snapshot() if cfg.health else {}
        rows = []
        for m in integration_modes(cfg.env):
            status, detail = live.get(m.service, ("", ""))
            rows.append({"service": m.service, "mode": m.mode, "why": m.why, "built": m.built,
                         "status": status, "detail": detail})
        cap = float(cfg.env.get("LLM_MAX_SPEND_USD", "3.00") or 3.0)
        try:
            spend = SpendLedger(conn, cap, 0.05).summary()
        except psycopg.Error:
            spend = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "usd": 0.0}
        return {"rows": rows, "spend": spend, "cap": cap, "checking": cfg.health is not None and not live,
                "pct": min(100, round(100 * spend["usd"] / cap)) if cap else 0}
    page("/integrations", "integrations.html", "integrations", integrations_ctx)

    page("/controls", "controls.html", "controls", lambda conn, r: {
        "c": effective_controls(conn, cfg.env, cfg.pack_veto_seconds)})

    # --- actions: each needs a signed-in session and a CSRF token, then runs the system's own code ---
    Handle = Callable[[psycopg.Connection, dict[str, str], dict[str, str]], actions.Result]

    def post(path: str, handle: Handle,
             redirect: str | Callable[[dict[str, str]], str]):
        @router.post(path)
        async def handler(request: Request) -> Response:
            if not cfg.auth.enabled:
                return disabled()
            cookie = request.cookies.get(COOKIE)
            form = await request.form()
            if not cfg.auth.csrf_ok(cookie, str(form.get("csrf", ""))):
                return PlainTextResponse("Forbidden", status_code=403, headers=HEADERS)
            fields = {k: str(v) for k, v in form.items()}

            def run() -> actions.Result:
                conn = cfg.connect()
                conn.autocommit = True
                with conn:
                    return handle(conn, request.path_params, fields)
            try:
                result = await run_in_threadpool(run)
            except ValueError:                              # a malformed id or number: refused, not a crash
                result = actions.Result(False, "That request was not valid.")
            return back(redirect(fields) if callable(redirect) else redirect, result)

    def aid(p: dict[str, str]) -> UUID:
        return UUID(p["action_id"])

    post("/actions/{action_id}/cancel", lambda c, p, f: actions.cancel_send(c, cfg.clock, aid(p)), "/pending")
    post("/actions/{action_id}/approve",
         lambda c, p, f: actions.decide(c, cfg.clock, aid(p), True), "/pending")
    post("/actions/{action_id}/reject",
         lambda c, p, f: actions.decide(c, cfg.clock, aid(p), False), "/pending")
    post("/queue/{dead_letter_id}/retry",
         lambda c, p, f: actions.retry_job(c, cfg.clock, int(p["dead_letter_id"])), "/queue")
    post("/controls/kill", lambda c, p, f: actions.set_kill(c, cfg.clock, f.get("on") == "1"), "/controls")
    post("/controls/mode", lambda c, p, f: actions.set_mode(c, f.get("mode", "")), "/controls")
    post("/controls/veto", lambda c, p, f: actions.set_veto(c, f.get("seconds", "")), "/controls")

    # --- static files (our own CSS and JS only) -------------------------------------------------------------
    @router.get("/static/{name}")
    def static(name: str) -> Response:
        kinds = {"dashboard.css": "text/css", "dashboard.js": "text/javascript"}
        if name not in kinds:
            return PlainTextResponse("Not found", status_code=404)
        return Response((HERE / "static" / name).read_bytes(), media_type=kinds[name],
                        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

    return router
