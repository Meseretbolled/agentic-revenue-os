"""`revos doctor`: one honest status per service. Never prints a value, a header or a secret.

REAL OK      the real service answered a harmless read call.
REAL FAILED  we have credentials but the call failed; the reason is a short category.
MOCK         the credentials are missing, so the mock stands in.
"""

import imaplib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import httpx
import psycopg

from revos.adapters.hubspot.client import DEFAULT_BASE_URL, EU_BASE_URL, category_for
from revos.adapters.spend import SpendLedger

OK, FAILED, MOCK = "REAL OK", "REAL FAILED", "MOCK"
TIMEOUT = 10.0


@dataclass(frozen=True)
class Check:
    service: str
    status: str
    detail: str = ""

    def line(self) -> str:
        return f"{self.service:<14}{self.status}" + (f"  ({self.detail})" if self.detail else "")


def _present(env: Mapping[str, str], *names: str) -> bool:
    return all(env.get(n, "").strip() for n in names)


def _http_failure(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    return "network"


def check_postgres(env: Mapping[str, str], connect: Callable[[], psycopg.Connection]) -> Check:
    try:
        with connect() as conn:
            conn.execute("SELECT 1")
        return Check("Postgres", OK)
    except psycopg.errors.InvalidPassword:
        return Check("Postgres", FAILED, "auth")
    except psycopg.OperationalError as exc:
        return Check("Postgres", FAILED, "auth" if "authentication" in str(exc).lower() else "network")


def check_llm(env: Mapping[str, str], connect: Callable[[], psycopg.Connection]) -> Check:
    if env.get("LLM_MODE", "fake") != "live":
        return Check("LLM budget", MOCK, "LLM_MODE is fake: no spend")
    if not _present(env, "ANTHROPIC_API_KEY"):
        return Check("LLM budget", MOCK, "key missing")
    cap = float(env.get("LLM_MAX_SPEND_USD", "3.00"))
    try:
        with connect() as conn:
            spent = SpendLedger(conn, cap, 0.05).spent_usd()
    except psycopg.Error:
        return Check("LLM budget", FAILED, "spend ledger unreachable")
    if spent >= cap:
        return Check("LLM budget", FAILED, f"cap reached: ${spent:.2f} of ${cap:.2f}")
    return Check("LLM budget", OK, f"${spent:.2f} of ${cap:.2f} used; the model was not called")


def spend_line(env: Mapping[str, str], connect: Callable[[], psycopg.Connection]) -> str:
    """One doctor line: calls, tokens, dollars against the cap. Reads the ledger; never calls a model."""
    if env.get("LLM_MODE", "fake") != "live":
        return "no model calls (LLM_MODE is fake)"
    cap = float(env.get("LLM_MAX_SPEND_USD", "3.00"))
    try:
        with connect() as conn:
            conn.autocommit = True
            done = SpendLedger(conn, cap, 0.05).summary()
    except psycopg.Error:
        return "ledger unavailable (run `revos run` once to apply migrations)"
    return (f"{done['calls']} calls, {done['tokens_in']:,} tokens in / {done['tokens_out']:,} out, "
            f"${done['usd']:.4f} of ${cap:.2f} cap ({done['usd'] / cap:.1%})")


def probe_hubspot(env: Mapping[str, str], http: httpx.Client) -> Check:
    """FIRST question: does the service key work as a normal Bearer token on the contacts API?
    If the configured base URL answers 401, 403 or 404, try the other one."""
    if not _present(env, "HUBSPOT_TOKEN"):
        return Check("HubSpot", MOCK, "key missing")
    configured = env.get("HUBSPOT_BASE_URL", "").strip().rstrip("/") or DEFAULT_BASE_URL
    alternative = EU_BASE_URL if configured != EU_BASE_URL else DEFAULT_BASE_URL
    headers = {"Authorization": f"Bearer {env['HUBSPOT_TOKEN']}"}
    first_failure = ""
    for base in (configured, alternative):
        try:
            got = http.get(f"{base}/crm/v3/objects/contacts", params={"limit": 1}, headers=headers,
                           timeout=TIMEOUT)
        except httpx.HTTPError as exc:
            return Check("HubSpot", FAILED, _http_failure(exc))
        if got.status_code == 200:
            note = "" if base == configured else f"set HUBSPOT_BASE_URL={base}"
            return Check("HubSpot", OK, note)
        first_failure = first_failure or f"{category_for(got.status_code)}, status {got.status_code}"
        if got.status_code not in (401, 403, 404):
            break
    return Check("HubSpot", FAILED, first_failure)


def check_slack(env: Mapping[str, str], http: httpx.Client) -> Check:
    if not _present(env, "SLACK_BOT_TOKEN"):
        return Check("Slack", MOCK, "key missing")
    try:
        got = http.post("https://slack.com/api/auth.test",
                        headers={"Authorization": f"Bearer {env['SLACK_BOT_TOKEN']}"}, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        return Check("Slack", FAILED, _http_failure(exc))
    body = got.json() if got.status_code == 200 else {}
    if body.get("ok"):
        missing = [n for n in ("SLACK_SIGNING_SECRET", "SLACK_CHANNEL") if not env.get(n, "").strip()]
        return Check("Slack", OK, "".join(f"{n} not set; " for n in missing).rstrip("; ")
                     + (" (buttons will be refused)" if "SLACK_SIGNING_SECRET" in missing else ""))
    error = str(body.get("error", ""))
    auth = got.status_code in (401, 403) or error in {"invalid_auth", "token_revoked", "account_inactive",
                                                       "not_authed", "token_expired"}
    return Check("Slack", FAILED, "auth" if auth else category_for(got.status_code) if got.status_code != 200
                 else "api_error")


def imap_login(address: str, password: str) -> None:
    """Log in to Gmail over IMAP and straight out again. Nothing is read or sent."""
    client = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=TIMEOUT)
    try:
        client.login(address, password)
    finally:
        try:
            client.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def check_gmail(env: Mapping[str, str], login: Callable[[str, str], None] = imap_login) -> Check:
    if not _present(env, "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"):
        return Check("Gmail", MOCK, "key missing")
    try:
        login(env["GMAIL_ADDRESS"], env["GMAIL_APP_PASSWORD"])
    except imaplib.IMAP4.error:
        return Check("Gmail", FAILED, "auth")
    except TimeoutError:
        return Check("Gmail", FAILED, "timeout")
    except OSError:
        return Check("Gmail", FAILED, "network")
    note = "" if _present(env, "PROSPECT_TEST_EMAIL") else "PROSPECT_TEST_EMAIL not set"
    return Check("Gmail", OK, note)


def check_fireflies(env: Mapping[str, str], http: httpx.Client) -> Check:
    if not _present(env, "FIREFLIES_API_KEY"):
        return Check("Fireflies", MOCK, "key missing")
    try:
        got = http.post("https://api.fireflies.ai/graphql", json={"query": "{ user { user_id } }"},
                        headers={"Authorization": f"Bearer {env['FIREFLIES_API_KEY']}"}, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        return Check("Fireflies", FAILED, _http_failure(exc))
    if got.status_code == 200 and not got.json().get("errors"):
        return Check("Fireflies", OK)
    reason = "auth" if got.status_code in (200, 401, 403) else category_for(got.status_code)
    return Check("Fireflies", FAILED, reason)


def check_dropbox_sign(env: Mapping[str, str], http: httpx.Client) -> Check:
    if not _present(env, "DROPBOX_SIGN_API_KEY"):
        return Check("Dropbox Sign", MOCK, "key missing")
    if env.get("DROPBOX_SIGN_TEST_MODE", "true").strip().lower() != "true":
        return Check("Dropbox Sign", FAILED, "unsafe config: test mode must stay on")
    try:
        got = http.get("https://api.hellosign.com/v3/account", auth=(env["DROPBOX_SIGN_API_KEY"], ""),
                       timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        return Check("Dropbox Sign", FAILED, _http_failure(exc))
    return Check("Dropbox Sign", OK) if got.status_code == 200 else Check(
        "Dropbox Sign", FAILED, category_for(got.status_code))


def check_tavily(env: Mapping[str, str], http: httpx.Client) -> Check:
    if not _present(env, "TAVILY_API_KEY"):
        return Check("Tavily", MOCK, "key missing")
    key = env["TAVILY_API_KEY"].strip().strip("\"'")
    try:
        got = http.post("https://api.tavily.com/search", json={"query": "revos doctor", "max_results": 1},
                        headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        return Check("Tavily", FAILED, _http_failure(exc))
    if got.status_code == 200:
        return Check("Tavily", OK, "one search credit used")
    if got.status_code in (401, 403):      # Tavily answers a bad or revoked key with 401 or 403
        return Check("Tavily", FAILED, "key rejected")
    return Check("Tavily", FAILED, "plan_limit" if got.status_code in (432, 433)
                 else category_for(got.status_code))


def check_tunnel(env: Mapping[str, str], http: httpx.Client) -> Check:
    if not _present(env, "PUBLIC_BASE_URL"):
        return Check("Tunnel URL", MOCK, "PUBLIC_BASE_URL is not set: Slack buttons will not work")
    try:
        got = http.get(env["PUBLIC_BASE_URL"].rstrip("/") + "/healthz", timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        return Check("Tunnel URL", FAILED, _http_failure(exc))
    return Check("Tunnel URL", OK) if got.status_code == 200 else Check(
        "Tunnel URL", FAILED, f"{category_for(got.status_code)}, status {got.status_code}")


def run_checks(env: Mapping[str, str] | None = None, *, http: httpx.Client | None = None,
               connect: Callable[[], psycopg.Connection] | None = None,
               login: Callable[[str, str], None] = imap_login) -> list[Check]:
    from revos.db import connect as default_connect
    env = env if env is not None else os.environ
    client = http or httpx.Client()
    open_db = connect or default_connect
    return [check_postgres(env, open_db), check_llm(env, open_db), probe_hubspot(env, client),
            check_slack(env, client), check_gmail(env, login), check_fireflies(env, client),
            check_dropbox_sign(env, client), check_tavily(env, client), check_tunnel(env, client)]
