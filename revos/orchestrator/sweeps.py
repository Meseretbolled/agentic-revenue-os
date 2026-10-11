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


def digest_summary(conn: psycopg.Connection, clock: Clock) -> dict[str, object]:
    """What the daily digest says, counted from the database (the last 24 hours where it says so)."""
    since = clock.now() - timedelta(hours=24)

    def count(sql: str, *args: object) -> int:
        return conn.execute(sql, args).fetchone()[0]

    return {
        "deals_by_stage": dict(conn.execute("SELECT stage, count(*) FROM deals GROUP BY stage").fetchall()),
        "emails_sent": count("SELECT count(*) FROM messages WHERE direction = 'outbound' "
                             "AND created_at >= %s", since),
        "replies": count("SELECT count(*) FROM messages WHERE direction = 'inbound' AND created_at >= %s",
                         since),
        "vetoes": count("SELECT count(*) FROM actions WHERE decision = 'vetoed' AND decided_at >= %s", since),
        "held_sends": count("SELECT count(*) FROM actions WHERE status = 'approved' AND "
                            "hold_reason IS NOT NULL"),
        "deferred_sends": count("SELECT count(*) FROM actions WHERE status = 'approved' AND run_at > %s",
                                clock.now()),
        "awaiting_approval": count("SELECT count(*) FROM actions WHERE status = 'awaiting_approval'"),
        "dead_letters": count("SELECT count(*) FROM dead_letters WHERE retried_at IS NULL"),
        "held_replies": count("SELECT count(*) FROM held_replies WHERE resolved_at IS NULL"),
        "new_lessons": count("SELECT count(*) FROM lessons WHERE created_at >= %s", since),
        "deals_stalled": count("SELECT count(DISTINCT deal_id) FROM audit_log WHERE event = "
                               "'tracking.stalled' AND at >= %s", since),
    }


def digest_text(day: str, summary: dict[str, object]) -> tuple[str, list[dict[str, object]]]:
    stages = summary["deals_by_stage"]
    by_stage = ", ".join(f"{k}: {v}" for k, v in sorted(stages.items())) or "no leads yet"   # type: ignore[union-attr]

    def row(label: str, key: str) -> str:
        return f"{label}: *{summary[key]}*"

    lines = [row("Emails sent (24h)", "emails_sent"), row("Replies (24h)", "replies"),
             row("Vetoes (24h)", "vetoes"), row("Held sends", "held_sends"),
             row("Deferred sends", "deferred_sends"), row("Waiting for approval", "awaiting_approval"),
             row("Dead letters", "dead_letters"), row("Held replies", "held_replies"),
             row("New lessons (24h)", "new_lessons"), row("Deals stalled (24h)", "deals_stalled")]
    blocks: list[dict[str, object]] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Revos daily digest, {day}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Leads by stage*\n{by_stage}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}]
    return f"Revos daily digest, {day}", blocks


def digest(conn: psycopg.Connection, clock: Clock, notifier: object = None) -> bool:
    """One summary per day: an audit row, and (when Slack is wired) one Slack message. Each half is
    idempotent on its own, so a retry after a Slack failure posts it once and writes no second row."""
    day = clock.now().date().isoformat()
    wrote = False
    with conn.transaction():
        if not conn.execute("SELECT 1 FROM audit_log WHERE event = 'tracking.digest' AND detail->>'day' = %s",
                            (day,)).fetchone():
            summary = digest_summary(conn, clock)
            write_audit(conn, "tracking", "tracking.digest", None, {"day": day, **summary})
            wrote = True
    if notifier is not None:
        post_digest(conn, clock, notifier, day)
    return wrote


def post_digest(conn: psycopg.Connection, clock: Clock, notifier, day: str) -> bool:
    """Post the day's digest to Slack unless it was already posted."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"digest:{day}",))
        if conn.execute("SELECT 1 FROM slack_messages WHERE kind = 'digest' AND ref = %s", (day,)).fetchone():
            return False
        row = conn.execute("SELECT detail FROM audit_log WHERE event = 'tracking.digest' AND "
                           "detail->>'day' = %s", (day,)).fetchone()
        summary = {k: v for k, v in (row[0] if row else digest_summary(conn, clock)).items() if k != "day"}
        text, blocks = digest_text(day, summary)
        posted = notifier.slack.post(notifier.channel, text, blocks)
        conn.execute("INSERT INTO slack_messages (kind, ref, channel, ts, state) "
                     "VALUES ('digest', %s, %s, %s, 'posted')", (day, posted.channel, posted.ts))
    return True
