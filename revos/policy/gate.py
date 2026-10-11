"""The send gate: the last check before anything leaves. It trusts nothing the caller says.

Everything is read from the database inside one transaction: the action's decision, the
contact's address and suppression flag, the kill switch. The counters are claimed in the same
transaction as the send, so a send that fails gives its slot back.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from revos.domain.errors import ActionNotApproved
from revos.policy.audit import write_audit
from revos.policy.config import OUTBOUND_TYPES, PolicyConfig
from revos.policy.recipients import recipient_allowed
from revos.policy.settings import kill_switch_on
from revos.policy.windows import next_open, zone
from revos.ports.clock import Clock
from revos.ports.provider import ActionProvider
from revos.runtime.outbox import execute_action


@dataclass(frozen=True)
class GateResult:
    status: str                         # sent | wait | deferred | refused
    reason: str | None = None
    provider_id: str | None = None
    run_at: datetime | None = None


class _CapReached(Exception):
    def __init__(self, scope: str) -> None:
        self.scope = scope


def _reserve(conn: psycopg.Connection, scope: str, day, cap: int) -> None:
    """Atomic compare-and-increment. Concurrent callers queue on the row lock and re-check."""
    row = None
    if cap > 0:
        row = conn.execute(
            "INSERT INTO send_counters (scope, day, count) VALUES (%s, %s, 1) "
            "ON CONFLICT (scope, day) DO UPDATE SET count = send_counters.count + 1 "
            "WHERE send_counters.count < %s RETURNING count",
            (scope, day, cap),
        ).fetchone()
    if row is None:
        raise _CapReached(scope)


def _hold(conn: psycopg.Connection, action: dict[str, Any], reason: str, *,
          run_at: datetime | None = None) -> None:
    if action["hold_reason"] != reason:       # one audit row per change, not one per poll
        write_audit(conn, "gate", "gate.held", action["deal_id"],
                    {"action_id": str(action["id"]), "reason": reason,
                     "run_at": run_at.isoformat() if run_at else None})
    conn.execute(
        "UPDATE actions SET hold_reason = %s, run_at = coalesce(%s, run_at) WHERE id = %s",
        (reason, run_at, action["id"]),
    )


def _cancel(conn: psycopg.Connection, action: dict[str, Any], reason: str, now: datetime) -> None:
    conn.execute(
        "UPDATE actions SET status = 'cancelled', decision = 'rejected', "
        "decision_by = 'policy:gate', decided_at = %s, hold_reason = %s WHERE id = %s",
        (now, reason, action["id"]),
    )
    write_audit(conn, "gate", "gate.refused", action["deal_id"],
                {"action_id": str(action["id"]), "reason": reason})


def send_with_gate(
    conn: psycopg.Connection,
    action_id: UUID,
    provider: ActionProvider,
    clock: Clock,
    config: PolicyConfig,
) -> GateResult:
    """Send the action if, and only if, every rule passes right now. Safe to call repeatedly."""
    now = clock.now()
    with conn.transaction():
        action = conn.cursor(row_factory=dict_row).execute(
            "SELECT a.*, c.email AS to_email, c.timezone AS to_tz, c.suppressed AS to_suppressed "
            "FROM actions a LEFT JOIN contacts c ON c.id = a.contact_id "
            "WHERE a.id = %s FOR UPDATE OF a",
            (action_id,),
        ).fetchone()
        if action is None:
            raise LookupError(f"no action {action_id}")
        if action["status"] == "sent":
            return GateResult("sent", provider_id=action["provider_id"])
        if action["type"] not in OUTBOUND_TYPES:
            return GateResult("refused", "not an outbound action")

        # 1. Kill switch: nothing moves, nothing is lost.
        if kill_switch_on(conn):
            _hold(conn, action, "kill_switch")
            return GateResult("wait", "kill_switch")

        # 2. The decision comes from the database row. Payload flags and arguments are ignored.
        if action["status"] != "approved" or action["decision"] != "approved" \
                or not action["decision_by"]:
            return GateResult("refused", f"not approved (status {action['status']})")

        # 3. Recipient facts are re-read from the contact row, never from the payload.
        if action["to_email"] is None:
            _cancel(conn, action, "no_recipient", now)
            return GateResult("refused", "no_recipient")
        if action["to_suppressed"]:
            _cancel(conn, action, "suppressed", now)
            return GateResult("refused", "suppressed")
        if config.demo_mode and not recipient_allowed(
                action["to_email"], config.allowed_recipients, config.plus_bases):
            _cancel(conn, action, "recipient_not_allowed", now)
            return GateResult("refused", "recipient_not_allowed")

        # 4. Timing: not due yet, then the recipient's local send window.
        if action["run_at"] > now:
            reason = action["hold_reason"] or "not_due"
            return GateResult("deferred", reason, run_at=action["run_at"])
        recipient_tz = zone(action["to_tz"], config.default_timezone)
        opens = next_open(now, recipient_tz, config.send_window)
        if opens > now:
            if config.send_window_bypass and config.demo_mode:
                # demo only: skip this one check, and say so every time. Nothing else is relaxed.
                write_audit(conn, "gate", "send_window.bypassed", action["deal_id"], {
                    "action_id": str(action_id),
                    "reason": f"demo bypass: the send window was closed until {opens.isoformat()}"})
            else:
                _hold(conn, action, "send_window", run_at=opens)
                return GateResult("deferred", "send_window", run_at=opens)

        # 5. Caps, claimed atomically. A rolled-back savepoint returns the domain slot.
        cap_tz = zone(config.cap_timezone, "UTC")
        day = now.astimezone(cap_tz).date()
        domain = action["to_email"].rsplit("@", 1)[-1].lower()
        try:
            with conn.transaction():
                _reserve(conn, f"domain:{domain}", day, config.domain_cap)
                _reserve(conn, "daily", day, config.daily_cap)
        except _CapReached as cap:
            midnight = datetime.combine(day + timedelta(days=1), time(0), tzinfo=cap_tz)
            retry_at = next_open(midnight, recipient_tz, config.send_window)
            _hold(conn, action, f"cap:{cap.scope}", run_at=retry_at)
            return GateResult("deferred", f"cap:{cap.scope}", run_at=retry_at)

        # 6. Send (checks the provider first), then clear any hold.
        provider_id = execute_action(conn, action_id, provider, clock)
        conn.execute("UPDATE actions SET sent_at = %s, hold_reason = NULL WHERE id = %s",
                     (now, action_id))
        return GateResult("sent", provider_id=provider_id)


EXECUTABLE_INTERNAL = frozenset({"crm_update", "crm_note", "book_meeting", "handoff"})


def run_approved_action(
    conn: psycopg.Connection,
    action_id: UUID,
    provider: ActionProvider,
    clock: Clock,
    config: PolicyConfig,
) -> GateResult:
    """Execute an approved action. Outbound types go through the full send gate; CRM writes go
    straight to the outbox, which still re-reads the decision from the database."""
    row = conn.execute("SELECT type FROM actions WHERE id = %s", (action_id,)).fetchone()
    if row is None:
        raise LookupError(f"no action {action_id}")
    if row[0] in OUTBOUND_TYPES:
        return send_with_gate(conn, action_id, provider, clock, config)
    if row[0] in EXECUTABLE_INTERNAL:
        try:
            return GateResult("sent", provider_id=execute_action(conn, action_id, provider, clock))
        except ActionNotApproved as exc:
            return GateResult("refused", str(exc))
    return GateResult("refused", "not an executable action")
