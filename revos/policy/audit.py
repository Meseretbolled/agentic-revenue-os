from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb


def write_audit(
    conn: psycopg.Connection, actor: str, event: str, deal_id: UUID | None, detail: dict[str, Any]
) -> None:
    conn.execute(
        "INSERT INTO audit_log (actor, event, deal_id, detail) VALUES (%s, %s, %s, %s)",
        (actor, event, deal_id, Jsonb(detail)),
    )
