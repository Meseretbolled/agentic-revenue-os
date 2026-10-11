"""`revos trace`: what happened, in order, from the audit log, plus replies held for a person."""

from typing import Any

import psycopg
from psycopg.rows import dict_row


def audit_trail(conn: psycopg.Connection, deal_id: str | None = None,
                limit: int = 40) -> list[dict[str, Any]]:
    where, args = ("WHERE deal_id = %s", (deal_id, limit)) if deal_id else ("", (limit,))
    rows = conn.cursor(row_factory=dict_row).execute(
        f"SELECT id, at, actor, event, deal_id, detail FROM audit_log {where} ORDER BY id DESC LIMIT %s",
        args).fetchall()
    return list(reversed(rows))


def held_replies(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Replies that could belong to more than one deal. Never attached to any; a person decides."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT message_id, from_addr, subject, received_at, reason, candidate_deal_ids FROM held_replies "
        "WHERE resolved_at IS NULL ORDER BY id").fetchall()
