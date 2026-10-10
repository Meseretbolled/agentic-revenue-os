"""The only writer of deal stage."""

from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from revos.domain.errors import DealNotFound, ExitCriteriaNotMet, IllegalTransition, StaleDeal
from revos.domain.stages import DealStage, ExitCriteria, can_transition

_COLUMN_FIELDS = ("tier", "total_score", "score_reason", "hubspot_deal_id")


def _is_present(value: object) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _missing_fields(deal: dict, required: list[str] | tuple[str, ...]) -> list[str]:
    extra = deal["fields"] or {}
    missing = []
    for name in required:
        value = deal[name] if name in _COLUMN_FIELDS else extra.get(name)
        if not _is_present(value):
            missing.append(name)
    return missing


def transition_deal(
    conn: psycopg.Connection,
    deal_id: UUID,
    to_stage: DealStage,
    *,
    actor: str,
    reason: str,
    exit_criteria: ExitCriteria | None = None,
    expected_version: int | None = None,
) -> int:
    """Move a deal to `to_stage` and write the audit row in the same transaction.

    Raises IllegalTransition, ExitCriteriaNotMet, StaleDeal or DealNotFound; in each
    case nothing is changed. Returns the deal's new version.
    """
    with conn.transaction():
        cur = conn.cursor(row_factory=dict_row)
        deal = cur.execute("SELECT * FROM deals WHERE id = %s FOR UPDATE", (deal_id,)).fetchone()
        if deal is None:
            raise DealNotFound(str(deal_id))
        if expected_version is not None and deal["version"] != expected_version:
            raise StaleDeal(f"deal {deal_id} is at version {deal['version']}")

        src = DealStage(deal["stage"])
        if not can_transition(src, to_stage):
            raise IllegalTransition(f"{src} -> {to_stage} is not allowed")

        missing = _missing_fields(deal, (exit_criteria or {}).get(to_stage, ()))
        if missing:
            raise ExitCriteriaNotMet(to_stage, missing)

        # Opens the DB guard for this statement only (see guard_deal_stage trigger).
        cur.execute("SELECT set_config('revos.allow_stage_change', 'on', true)")
        new_version = cur.execute(
            "UPDATE deals SET stage = %s, version = version + 1, updated_at = now() "
            "WHERE id = %s RETURNING version",
            (to_stage.value, deal_id),
        ).fetchone()["version"]
        cur.execute("SELECT set_config('revos.allow_stage_change', 'off', true)")

        cur.execute(
            "INSERT INTO audit_log (actor, event, deal_id, detail) VALUES (%s, %s, %s, %s)",
            (
                actor,
                "deal.stage_changed",
                deal_id,
                Jsonb({"from": src.value, "to": to_stage.value, "reason": reason,
                       "version": new_version}),
            ),
        )
        return new_version
