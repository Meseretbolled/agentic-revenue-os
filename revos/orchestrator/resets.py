"""Closing deals in bulk for a demo reset. Lives with the orchestrator because only it moves stages."""

import psycopg

from revos.domain.stages import DealStage
from revos.orchestrator.transitions import transition_deal

CLOSED = ("closed_won", "closed_lost", "do_not_contact", "handoff")


def close_open_deals(conn: psycopg.Connection, actor: str, reason: str) -> int:
    """Move every deal that is still open to closed_lost, through the state machine."""
    open_deals = conn.execute("SELECT id FROM deals WHERE stage <> ALL(%s)", (list(CLOSED),)).fetchall()
    for (deal_id,) in open_deals:
        transition_deal(conn, deal_id, DealStage.CLOSED_LOST, actor=actor, reason=reason)
    return len(open_deals)
