"""Run scope: which deal and segment an agent run is allowed to touch. Fails closed."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from revos.domain.errors import RevosError


class ToolBlocked(RevosError):
    """A tool call was refused. The message is safe to show the model."""


@dataclass(frozen=True)
class RunScope:
    deal_id: UUID
    segment_id: str
    contact_id: UUID | None
    company_id: UUID | None
    company_domain: str | None


ScopeProvider = Callable[[], RunScope | None]


def resolve_scope(conn: psycopg.Connection, deal_id: UUID) -> RunScope:
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT d.id, d.segment_id, d.contact_id, d.company_id, c.domain "
        "FROM deals d LEFT JOIN companies c ON c.id = d.company_id WHERE d.id = %s", (deal_id,)
    ).fetchone()
    if row is None:
        raise ToolBlocked("the run's deal does not exist")
    return RunScope(row["id"], row["segment_id"], row["contact_id"], row["company_id"],
                    (row["domain"] or "").lower() or None)


def fixed_scope(conn: psycopg.Connection, deal_id: UUID) -> ScopeProvider:
    """A provider for one run. If the scope cannot be resolved it returns None, and every tool
    call is then blocked."""
    try:
        scope: RunScope | None = resolve_scope(conn, deal_id)
    except Exception:  # noqa: BLE001 - anything unresolvable means no scope
        scope = None
    return lambda: scope


def as_uuid(args: dict[str, Any], name: str) -> UUID:
    try:
        return UUID(str(args[name]))
    except (KeyError, ValueError, TypeError) as exc:
        raise ToolBlocked(f"{name} is missing or not a valid id") from exc


def require_own(value: UUID, own: UUID | None, name: str) -> None:
    """The id must be the run's own. Same message whether it is foreign or unknown."""
    if own is None or value != own:
        raise ToolBlocked(f"{name} does not belong to this run")
