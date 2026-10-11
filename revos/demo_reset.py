"""Local half of `revos reset-demo`: stop everything that is queued but has not happened.

It cancels unsent actions and queued jobs; it deletes nothing (the audit log is append-only, and
what already happened stays on record). After it, a restarted `revos run` cannot send an old email."""

import psycopg
from psycopg.types.json import Jsonb

from revos.orchestrator.resets import close_open_deals

QUEUED_TYPES = ("lead.created", "action.send", "action.veto_expired", "lead.snooze_ended",
                "signature.event")


def cancel_pending_work(conn: psycopg.Connection) -> dict[str, int]:
    """Cancel unsent actions and queued jobs. Safe to repeat."""
    with conn.transaction():
        actions = conn.execute(
            "UPDATE actions SET status = 'cancelled', hold_reason = 'cancelled by reset-demo' "
            "WHERE status IN ('pending', 'awaiting_approval', 'approved') RETURNING deal_id, type").fetchall()
        jobs = conn.execute(
            "UPDATE jobs SET status = 'done', lease_until = NULL, last_error = 'cancelled by reset-demo' "
            "WHERE status = 'queued' AND type = ANY(%s) RETURNING id", (list(QUEUED_TYPES),)).fetchall()
        for deal_id, kind in actions:
            conn.execute("INSERT INTO audit_log (actor, event, deal_id, detail) VALUES "
                         "('operator', 'demo.reset', %s, %s)", (deal_id, Jsonb({"cancelled_action": kind})))
        # Close every deal that is still open, through the state machine, so an old demo deal can never
        # again be a candidate for a reply or a call (matching only considers live deals).
        closed = close_open_deals(conn, "operator", "reset-demo")
    return {"actions": len(actions), "jobs": len(jobs), "deals": closed}
