"""Handling a Slack button press. Everything here distrusts the payload.

* The request signature is checked over the RAW body (v0 HMAC-SHA256, constant-time, at most five
  minutes old) before anything is parsed.
* A button carries only an action id. What the action is, what state it is in and whether the
  button is allowed are all re-read from Postgres; a decision flag in the payload is never read.
* One press is handled once: the key (trigger id + action id) is stored first.
* Cancelling is a compare-and-set on the action's row, so it cannot both win with the send."""

import hashlib
import hmac
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import parse_qs
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from revos.obs import log
from revos.policy.audit import write_audit
from revos.policy.service import decide_by_human, veto_action
from revos.ports.clock import Clock
from revos.ports.slack import Slack
from revos.runtime.events import record_event
from revos.slack.blocks import ACTION_APPROVE, ACTION_CANCEL, ACTION_REJECT

MAX_AGE_SECONDS = 300
VERBS = {ACTION_CANCEL: "cancel", ACTION_APPROVE: "approve", ACTION_REJECT: "reject"}


class BadSignature(Exception):
    """The request is not from Slack (or is a replay of an old one)."""


def verify_signature(signing_secret: str, raw_body: bytes, timestamp: str | None, signature: str | None,
                     *, now: float | None = None) -> None:
    """Slack's v0 scheme. Raises BadSignature; the reason is for our logs, never for the caller."""
    if not signing_secret:
        raise BadSignature("no signing secret configured")
    if not timestamp or not signature:
        raise BadSignature("missing signature headers")
    try:
        age = abs((now if now is not None else time.time()) - int(timestamp))
    except ValueError:
        raise BadSignature("unreadable timestamp") from None
    if age > MAX_AGE_SECONDS:
        raise BadSignature("timestamp outside the five minute window")
    base = b"v0:" + timestamp.encode() + b":" + raw_body            # the raw body, never re-serialised
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise BadSignature("signature mismatch")


@dataclass(frozen=True)
class Press:
    user_id: str
    user_name: str
    verb: str                 # cancel | approve | reject
    action_id: UUID
    trigger_id: str
    channel: str


def parse_press(raw_body: bytes) -> Press | None:
    """The one button press in a block_actions payload, or None if it is anything else."""
    try:
        form = parse_qs(raw_body.decode("utf-8"), strict_parsing=True)
        payload = json.loads(form["payload"][0])
        if payload.get("type") != "block_actions":
            return None
        button = payload["actions"][0]
        verb = VERBS.get(button.get("action_id"))
        if verb is None:
            return None
        user = payload["user"]
        return Press(str(user["id"]), str(user.get("username") or user["id"]), verb,
                     UUID(str(button["value"])), str(payload["trigger_id"]),
                     str((payload.get("channel") or {}).get("id") or ""))
    except (KeyError, IndexError, ValueError, TypeError):
        return None


@dataclass(frozen=True)
class Outcome:
    status: str               # done | replay | refused | late | blocked | unknown | forbidden
    message: str = ""


class SlackInteractions:
    def __init__(self, conn_factory: Callable[[], psycopg.Connection], clock: Clock, slack: Slack,
                 allowed_users: Iterable[str] = ()) -> None:
        self._connect, self._clock, self._slack = conn_factory, clock, slack
        self._allowed = frozenset(u.strip() for u in allowed_users if u.strip())

    def handle(self, press: Press) -> Outcome:
        with self._connect() as conn:
            conn.autocommit = True
            return self._handle(conn, press)

    def _handle(self, conn: psycopg.Connection, press: Press) -> Outcome:
        key = f"{press.trigger_id}:{press.action_id}"
        if self._allowed and press.user_id not in self._allowed:
            if self._first(conn, key, press, "refused"):
                write_audit(conn, f"slack:{press.user_id}", "slack.refused_user", None, {
                    "action_id": str(press.action_id), "verb": press.verb})
                self._tell(press, "You are not allowed to use these buttons.")
                log("slack", "button refused: user not allowed", user=press.user_id)
            return Outcome("forbidden", "user not allowed")
        if not self._first(conn, key, press, "received"):
            log("slack", "replayed press ignored", user=press.user_id)
            return Outcome("replay", "already handled")

        try:
            outcome = self._decide(conn, press)
        except Exception:
            # let Slack's own retry through: the press never took effect
            conn.execute("DELETE FROM slack_interactions WHERE key = %s", (key,))
            raise
        conn.execute("UPDATE slack_interactions SET outcome = %s WHERE key = %s", (outcome.status, key))
        if outcome.status != "done":
            write_audit(conn, f"slack:{press.user_id}", f"slack.{outcome.status}", None, {
                "action_id": str(press.action_id), "verb": press.verb, "message": outcome.message})
            self._tell(press, outcome.message)
        log("slack", f"press {press.verb}: {outcome.status}", user=press.user_id,
            action_id=str(press.action_id))
        return outcome

    def _first(self, conn: psycopg.Connection, key: str, press: Press, outcome: str) -> bool:
        """Record the press; False if this exact press was already recorded."""
        return conn.execute(
            "INSERT INTO slack_interactions (key, user_id, action_id, verb, outcome) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (key) DO NOTHING RETURNING key",
            (key, press.user_id, press.action_id, press.verb, outcome)).fetchone() is not None

    def _decide(self, conn: psycopg.Connection, press: Press) -> Outcome:
        row = conn.cursor(row_factory=dict_row).execute(
            "SELECT id, status, decision, decision_by, veto_until, type FROM actions WHERE id = %s",
            (press.action_id,)).fetchone()
        if row is None:
            return Outcome("unknown", "That action no longer exists.")
        who = f"slack:{press.user_id}"
        now = self._clock.now()
        if press.verb == "cancel":
            if row["status"] != "pending":
                return Outcome("late", _late(row))
            try:
                veto_action(conn, self._clock, press.action_id, who)       # compare-and-set under a row lock
            except ValueError:
                return Outcome("late", "The window has closed: it is already being sent.")
            write_audit(conn, who, "slack.cancelled", self._deal(conn, press.action_id), {
                "action_id": str(press.action_id), "user": press.user_id, "at": now.isoformat()})
        else:
            if row["status"] == "cancelled" and row["decision_by"] == "policy":
                return Outcome("blocked", "This action is blocked by policy and cannot be approved "
                                          "or rejected.")
            if row["status"] != "awaiting_approval":
                return Outcome("late", _late(row))
            try:
                decide_by_human(conn, self._clock, press.action_id, who, press.verb == "approve",
                                f"{press.verb}d in Slack")
            except ValueError:
                return Outcome("late", "Someone else already decided this.")
            if press.verb == "approve":
                record_event(conn, f"approved-send:{press.action_id}", "action.send",
                             {"action_id": str(press.action_id)}, self._clock)
        record_event(conn, f"slack:{press.action_id}:{press.verb}", "slack.sync",
                     {"action_id": str(press.action_id)}, self._clock)
        return Outcome("done")

    @staticmethod
    def _deal(conn: psycopg.Connection, action_id: UUID):
        row = conn.execute("SELECT deal_id FROM actions WHERE id = %s", (action_id,)).fetchone()
        return row[0] if row else None

    def _tell(self, press: Press, text: str) -> None:
        if not press.channel or not text:
            return
        try:
            self._slack.ephemeral(press.channel, press.user_id, text)
        except Exception as exc:  # noqa: BLE001 - a failed courtesy message must not undo the decision
            log("slack", "could not send the ephemeral reply", level="warning", error=type(exc).__name__)


def _late(row: dict) -> str:
    status = row["status"]
    return {"sent": "Too late: it has already been sent.", "approved": "Too late: it is already being sent.",
            "cancelled": "It was already cancelled."}.get(status, f"Nothing to do: the action is {status}.")
