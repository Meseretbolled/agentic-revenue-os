"""Turns a PolicyDecision into the stored state of an action, and records human decisions."""

from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from revos.domain.enums import AutonomyLevel, Outcome
from revos.policy.audit import write_audit
from revos.policy.config import PolicyConfig
from revos.policy.engine import ActionRequest, PolicyDecision, decide
from revos.policy.settings import autonomy_mode, kill_switch_on
from revos.ports.clock import Clock


@dataclass(frozen=True)
class Proposal:
    action_id: UUID
    created: bool                       # False: this idempotency key already existed
    decision: PolicyDecision | None     # None when the key already existed


def propose_action(
    conn: psycopg.Connection,
    clock: Clock,
    config: PolicyConfig,
    *,
    key: str,
    request: ActionRequest,
    deal_id: UUID | None = None,
    contact_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> Proposal:
    """Decide what may happen to a proposed action and store it in that state.

    Nothing here sends anything. AUTO becomes approved (by 'policy'); AUTO_WITH_VETO waits for
    its window; NEEDS_APPROVAL waits for a person; BLOCKED, DENIED are cancelled for good.
    """
    now = clock.now()
    with conn.transaction():
        suppressed = False
        if contact_id is not None:
            row = conn.execute(
                "SELECT suppressed FROM contacts WHERE id = %s", (contact_id,)
            ).fetchone()
            suppressed = bool(row and row[0])
        decision = decide(
            replace(request, contact_suppressed=request.contact_suppressed or suppressed),
            config,
            mode=autonomy_mode(conn),
            kill_switch=kill_switch_on(conn),
        )

        status, verdict, by, veto_until = "pending", None, None, None
        match decision.outcome:
            case Outcome.AUTO:
                status, verdict, by = "approved", "approved", "policy"
            case Outcome.AUTO_WITH_VETO:
                veto_until = now + timedelta(seconds=config.veto_window_seconds)
            case Outcome.NEEDS_APPROVAL:
                status = "awaiting_approval"
            case Outcome.DRAFT_ONLY:
                status = "draft"
            case Outcome.BLOCKED | Outcome.DENIED:
                status, verdict, by = "cancelled", "rejected", "policy"

        row = conn.execute(
            "INSERT INTO actions (idempotency_key, deal_id, contact_id, type, autonomy_level, "
            "status, decision, decision_by, decided_at, veto_until, hold_reason, payload, run_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
            (
                key, deal_id, contact_id, request.action_type,
                (decision.level or AutonomyLevel.NEEDS_APPROVAL).value,
                status, verdict, by, now if verdict else None, veto_until,
                "; ".join(decision.reasons) if verdict == "rejected" else None,
                Jsonb(payload or {}), now,
            ),
        ).fetchone()
        if row is None:
            existing = conn.execute(
                "SELECT id FROM actions WHERE idempotency_key = %s", (key,)
            ).fetchone()
            return Proposal(existing[0], False, None)

        write_audit(conn, "policy", "policy.decided", deal_id, {
            "action_id": str(row[0]), "action_type": request.action_type,
            "outcome": decision.outcome.value, "reasons": list(decision.reasons),
            "mode": autonomy_mode(conn).value, "held": decision.held,
        })
        return Proposal(row[0], True, decision)


def _locked_action(conn: psycopg.Connection, action_id: UUID) -> dict[str, Any]:
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT * FROM actions WHERE id = %s FOR UPDATE", (action_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no action {action_id}")
    return row


def _record(conn: psycopg.Connection, action: dict[str, Any], decision: str, by: str,
            reason: str | None, now) -> None:
    status = "approved" if decision == "approved" else "cancelled"
    conn.execute(
        "UPDATE actions SET status = %s, decision = %s, decision_by = %s, decided_at = %s "
        "WHERE id = %s",
        (status, decision, by, now, action["id"]),
    )
    conn.execute(
        "INSERT INTO approvals (action_id, decision, decided_by, reason, decided_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (action["id"], decision, by, reason, now),
    )
    write_audit(conn, by, f"action.{decision}", action["deal_id"],
                {"action_id": str(action["id"]), "reason": reason})


def decide_by_human(conn: psycopg.Connection, clock: Clock, action_id: UUID, approver: str,
                    approve: bool, reason: str | None = None) -> None:
    """A person approves or rejects an action that was waiting for approval."""
    with conn.transaction():
        action = _locked_action(conn, action_id)
        if action["status"] != "awaiting_approval":
            raise ValueError(f"action is {action['status']}, not awaiting approval")
        _record(conn, action, "approved" if approve else "rejected", approver, reason, clock.now())


def veto_action(conn: psycopg.Connection, clock: Clock, action_id: UUID, by: str) -> None:
    """Cancel an auto_with_veto action while its window is still open."""
    with conn.transaction():
        action = _locked_action(conn, action_id)
        now = clock.now()
        window_end = action["veto_until"]
        if action["status"] != "pending" or window_end is None or now >= window_end:
            raise ValueError("no open veto window for this action")
        _record(conn, action, "vetoed", by, "vetoed in window", now)


def release_veto_expired(conn: psycopg.Connection, clock: Clock) -> list[UUID]:
    """Approve (as 'policy') every auto_with_veto action whose window ended without a veto."""
    released = []
    now = clock.now()
    with conn.transaction():
        rows = conn.cursor(row_factory=dict_row).execute(
            "SELECT * FROM actions WHERE status = 'pending' AND veto_until <= %s "
            "ORDER BY veto_until FOR UPDATE SKIP LOCKED", (now,)).fetchall()
        for action in rows:
            _record(conn, action, "approved", "policy:veto_window", "veto window expired", now)
            released.append(action["id"])
    return released
