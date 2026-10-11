from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from revos.obs import log


def write_audit(
    conn: psycopg.Connection, actor: str, event: str, deal_id: UUID | None, detail: dict[str, Any]
) -> None:
    conn.execute(
        "INSERT INTO audit_log (actor, event, deal_id, detail) VALUES (%s, %s, %s, %s)",
        (actor, event, deal_id, Jsonb(detail)),
    )
    log("audit", event, deal_id=deal_id, actor=actor, **{k: v for k, v in detail.items()
                                                       if k in LOGGED_DETAIL})


# the audit detail keys that are safe and useful in a log line (never message bodies)
LOGGED_DETAIL = {"action_id", "action_type", "type", "outcome", "reasons", "from", "to", "reason",
                 "provider_id", "message_id", "by", "agent", "step", "status", "lesson_ids", "candidates",
                 "recovered_from_provider", "decision_by"}
