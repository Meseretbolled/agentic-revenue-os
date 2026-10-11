"""Dashboard sign-in: a shared token, a signed HttpOnly session cookie and a CSRF token on every POST.

* The token is read from DASHBOARD_TOKEN and compared in constant time (on SHA-256 digests, so the length is
  not leaked either). It is never rendered, logged or put in a cookie.
* The session cookie is `expiry.nonce.signature`, signed with a key derived from the token. Changing the
  token signs everyone out. HttpOnly, SameSite=Strict, scoped to /dashboard.
* The CSRF token is an HMAC of the session cookie, so a page from another origin cannot know it. The login
  form has its own, bound to a one-time pre-login cookie.
* Failed sign-ins are rate limited per client."""

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

COOKIE = "revos_session"
PRE_COOKIE = "revos_pre"
SESSION_SECONDS = 8 * 3600
MAX_FAILURES, FAILURE_WINDOW = 5, 300


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


class Auth:
    def __init__(self, token: str, *, now=time.time) -> None:
        self._token = token
        self._key = hashlib.sha256(b"revos-dashboard-session|" + token.encode()).digest()
        self._now = now
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    @property
    def enabled(self) -> bool:
        return bool(self._token)

    # --- the token ----------------------------------------------------------------------------------------
    def token_ok(self, given: str) -> bool:
        return self.enabled and hmac.compare_digest(_digest(given), _digest(self._token))

    def _sign(self, text: str) -> str:
        return hmac.new(self._key, text.encode(), hashlib.sha256).hexdigest()

    # --- rate limit ---------------------------------------------------------------------------------------
    def locked_out(self, client: str) -> bool:
        window = self._failures[client]
        while window and window[0] < self._now() - FAILURE_WINDOW:
            window.popleft()
        return len(window) >= MAX_FAILURES

    def note_failure(self, client: str) -> None:
        self._failures[client].append(self._now())

    # --- the session --------------------------------------------------------------------------------------
    def new_session(self) -> str:
        body = f"{int(self._now()) + SESSION_SECONDS}.{secrets.token_hex(8)}"
        return f"{body}.{self._sign('session|' + body)}"

    def session_ok(self, cookie: str | None) -> bool:
        if not self.enabled or not cookie:
            return False
        body, _, signature = cookie.rpartition(".")
        expiry = body.partition(".")[0]
        if not expiry.isdigit() or int(expiry) < self._now():
            return False
        return hmac.compare_digest(signature, self._sign("session|" + body))

    def csrf_for(self, session_cookie: str) -> str:
        return self._sign("csrf|" + session_cookie)

    def csrf_ok(self, session_cookie: str | None, given: str | None) -> bool:
        return bool(session_cookie and given and self.session_ok(session_cookie)
                    and hmac.compare_digest(given, self.csrf_for(session_cookie)))

    # --- the login form (no session yet) --------------------------------------------------------------------
    def new_pre(self) -> str:
        return secrets.token_hex(16)

    def login_csrf(self, pre_cookie: str) -> str:
        return self._sign("login|" + pre_cookie)

    def login_csrf_ok(self, pre_cookie: str | None, given: str | None) -> bool:
        return bool(pre_cookie and given and hmac.compare_digest(given, self.login_csrf(pre_cookie)))
