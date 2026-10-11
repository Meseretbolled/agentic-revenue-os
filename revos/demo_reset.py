"""Local half of `revos reset-demo`: stop everything that is queued but has not happened.

It cancels unsent actions and queued jobs; it deletes nothing (the audit log is append-only, and
what already happened stays on record). After it, a restarted `revos run` cannot send an old email."""

import psycopg
from psycopg.types.json import Jsonb

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
    return {"actions": len(actions), "jobs": len(jobs)}
