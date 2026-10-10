"""Logging of quarantined inbound text: what matched, never obeyed, shown to a person."""

from uuid import UUID

import psycopg

from revos.domain.untrusted import UntrustedText
from revos.policy.audit import write_audit


def record_quarantine(conn: psycopg.Connection, content: UntrustedText,
                      deal_id: UUID | None = None) -> None:
    review = content.for_human()
    write_audit(conn, "quarantine", "inbound.quarantined", deal_id, {
        "kind": review["kind"], "origin": review["origin"], "boundary": review["boundary"],
        "findings": review["findings"], "hidden_text_pieces": len(content.hidden_text),
        "action": "held for a person; not given to any model",
    })
