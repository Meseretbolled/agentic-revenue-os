"""Outbox: every side effect is an actions row with a unique key, and the provider is
asked before any retry."""

from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from revos.domain.errors import ActionNotApproved
from revos.obs import log
from revos.ports.clock import Clock
from revos.ports.provider import ActionProvider


def enqueue_action(
    conn: psycopg.Connection,
    key: str,
    type_: str,
    payload: dict[str, Any],
    *,
    autonomy_level: str,
    deal_id: UUID | None = None,
    contact_id: UUID | None = None,
    risk: str = "low",
) -> UUID:
    """Create the action once. Calling again with the same key returns the same id."""
    with conn.transaction():
        conn.execute(
            "INSERT INTO actions (idempotency_key, type, payload, autonomy_level, deal_id, "
            "contact_id, risk) VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (key, type_, Jsonb(payload), autonomy_level, deal_id, contact_id, risk),
        )
        row = conn.execute("SELECT id FROM actions WHERE idempotency_key = %s", (key,)).fetchone()
        return row[0]


def execute_action(
    conn: psycopg.Connection, action_id: UUID, provider: ActionProvider, clock: Clock
) -> str:
    """Perform the action at most once and return the provider id.

    The decision is re-read from the database here; nothing the caller says counts. The row
    stays locked while we talk to the provider, so two workers cannot both send. If the
    provider already has this key (an earlier attempt got through but we crashed before
    recording it), we record that result and do NOT call perform again.
    """
    with conn.transaction():
        cur = conn.cursor(row_factory=dict_row)
        # to_email comes from the contact row, never from the payload.
        action = cur.execute(
            "SELECT a.*, c.email AS to_email, "
            "(SELECT d.fields->>'signer_email' FROM deals d WHERE d.id = a.deal_id) AS signer_email "
            "FROM actions a "
            "LEFT JOIN contacts c ON c.id = a.contact_id WHERE a.id = %s FOR UPDATE OF a",
            (action_id,),
        ).fetchone()
        if action is None:
            raise LookupError(f"no action {action_id}")
        if action["status"] == "sent":
            return action["provider_id"]
        if action["status"] != "approved" or action["decision"] != "approved":
            raise ActionNotApproved(f"action {action_id} is {action['status']}, not approved")

        find_for = getattr(provider, "find_for", None)
        existing = (find_for(action) if find_for else provider.find(action["idempotency_key"]))
        if existing is not None:
            provider_id, recovered = existing, True
        else:
            provider_id, recovered = provider.perform(action), False

        cur.execute(
            "UPDATE actions SET status = 'sent', provider_id = %s WHERE id = %s",
            (provider_id, action_id),
        )
        cur.execute(
            "INSERT INTO audit_log (actor, event, deal_id, detail) "
            "VALUES ('outbox', 'action.sent', %s, %s)",
            (action["deal_id"], Jsonb({"action_id": str(action_id), "type": action["type"],
                                      "provider_id": provider_id,
                                      "recovered_from_provider": recovered,
                                      "decision_by": action["decision_by"]})),
        )
        log("outbox", "action.sent", deal_id=action["deal_id"], action_id=str(action_id),
            type=action["type"], provider_id=provider_id, recovered_from_provider=recovered)
        return provider_id
