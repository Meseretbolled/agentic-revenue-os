"""The real Slack adapter: chat.postMessage, chat.update, chat.postEphemeral over HTTPS.

The bot token is held privately and never appears in an error or a repr. Errors carry Slack's own
error code (`channel_not_found`, `not_in_channel`, `invalid_auth`) and nothing else from the response.
Slack answers 200 with ok:false for most failures, so the body's `ok` is what counts."""

import time
from collections.abc import Callable

import httpx

from revos.domain.errors import PermanentError, RevosError
from revos.ports.slack import Blocks, Posted

API = "https://slack.com/api"
PERMANENT = {"channel_not_found", "not_in_channel", "is_archived", "invalid_auth", "not_authed",
             "token_revoked", "account_inactive", "missing_scope", "invalid_blocks", "msg_too_long",
             "message_not_found", "cant_update_message", "invalid_arguments", "no_text"}


class SlackError(PermanentError):
    def __init__(self, method: str, code: str) -> None:
        self.code = code
        super().__init__(f"Slack {method} failed: {code}")


class SlackUnavailable(RevosError):
    def __init__(self, method: str, why: str) -> None:
        super().__init__(f"Slack {method} unavailable: {why}")


class SlackAdapter:
    def __init__(self, token: str, *, http: httpx.Client | None = None, max_retries: int = 3,
                 sleep: Callable[[float], None] = time.sleep, timeout: float = 15.0) -> None:
        self._token = token
        self._http = http or httpx.Client(timeout=timeout)
        self._retries, self._sleep, self._timeout = max_retries, sleep, timeout

    def __repr__(self) -> str:
        return "SlackAdapter()"

    def _call(self, method: str, body: dict) -> dict:
        reason = "network"
        for attempt in range(self._retries + 1):
            if attempt:
                self._sleep(min(30.0, 0.5 * 2 ** attempt))
            try:
                response = self._http.post(
                    f"{API}/{method}", json=body, timeout=self._timeout,
                    headers={"Authorization": f"Bearer {self._token}",
                             "Content-Type": "application/json; charset=utf-8"})
            except httpx.HTTPError:
                reason = "network"
                continue
            if response.status_code == 429:
                reason = "rate_limited"
                retry = response.headers.get("Retry-After", "")
                if retry.isdigit():
                    self._sleep(min(30.0, float(retry)))
                continue
            if response.status_code >= 500:
                reason = "server_error"
                continue
            try:
                data = response.json()
            except ValueError:
                raise SlackUnavailable(method, "unreadable response") from None
            if data.get("ok"):
                return data
            code = str(data.get("error", "unknown_error"))
            if code in PERMANENT or response.status_code in (401, 403):
                raise SlackError(method, code)
            raise SlackUnavailable(method, code)
        raise SlackUnavailable(method, reason)

    def post(self, channel: str, text: str, blocks: Blocks) -> Posted:
        data = self._call("chat.postMessage", {"channel": channel, "text": text, "blocks": list(blocks),
                                               "unfurl_links": False, "unfurl_media": False})
        return Posted(str(data.get("channel", channel)), str(data["ts"]))

    def update(self, channel: str, ts: str, text: str, blocks: Blocks) -> None:
        self._call("chat.update", {"channel": channel, "ts": ts, "text": text, "blocks": list(blocks)})

    def ephemeral(self, channel: str, user: str, text: str) -> None:
        self._call("chat.postEphemeral", {"channel": channel, "user": user, "text": text})
