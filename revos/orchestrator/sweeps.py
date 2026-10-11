"""The recurring tracking jobs. Deterministic, idempotent, and read-mostly: they write audit rows
only (the Slack digest message arrives with Step 10)."""

from datetime import timedelta

import psycopg
from psycopg.rows import dict_row

from revos.policy.audit import write_audit
from revos.ports.clock import Clock

STALL_AFTER = timedelta(days=3)


def stall_scan(conn: psycopg.Connection, clock: Clock, after: timedelta = STALL_AFTER) -> int:
    """Flag deals in outreach whose last email went out `after` ago with no reply since.
    Each stalled message is flagged once, however often the scan runs."""
    flagged = 0
    cutoff = clock.now() - after
    for row in conn.cursor(row_factory=dict_row).execute(
            "SELECT d.id, m.message_id, m.created_at FROM deals d JOIN LATERAL ("
            "  SELECT message_id, created_at FROM messages WHERE deal_id = d.id AND direction = 'outbound' "
            "  ORDER BY id DESC LIMIT 1) m ON true "
            "WHERE d.stage = 'outreach' AND m.created_at < %s AND NOT EXISTS ("
            "  SELECT 1 FROM messages r WHERE r.deal_id = d.id AND r.direction = 'inbound' "
            "  AND r.created_at > m.created_at)", (cutoff,)).fetchall():
        with conn.transaction():
            seen = conn.execute("SELECT 1 FROM audit_log WHERE event = 'tracking.stalled' AND deal_id = %s "
                                "AND detail->>'message_id' = %s", (row["id"], row["message_id"])).fetchone()
            if not seen:
                write_audit(conn, "tracking", "tracking.stalled", row["id"], {
                    "message_id": row["message_id"],
                    "reason": f"no reply since {row['created_at']:%Y-%m-%d}"})
                flagged += 1
    return flagged


def digest(conn: psycopg.Connection, clock: Clock) -> bool:
    """One summary row per day: deals by stage, what waits for a person, what failed."""
    day = clock.now().date().isoformat()
    with conn.transaction():
        if conn.execute("SELECT 1 FROM audit_log WHERE event = 'tracking.digest' AND detail->>'day' = %s",
                        (day,)).fetchone():
            return False
        stages = dict(conn.execute("SELECT stage, count(*) FROM deals GROUP BY stage").fetchall())
        waiting = conn.execute("SELECT count(*) FROM actions WHERE status = 'awaiting_approval'"
                               ).fetchone()[0]
        dead = conn.execute("SELECT count(*) FROM dead_letters WHERE retried_at IS NULL").fetchone()[0]
        held = conn.execute("SELECT count(*) FROM held_replies WHERE resolved_at IS NULL").fetchone()[0]
        write_audit(conn, "tracking", "tracking.digest", None, {
            "day": day, "deals_by_stage": stages, "awaiting_approval": waiting, "dead_letters": dead,
            "held_replies": held})
    return True
