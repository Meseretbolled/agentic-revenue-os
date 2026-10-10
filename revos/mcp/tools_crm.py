from typing import Any

from psycopg.rows import dict_row

from revos.mcp.registry import ToolContext, ToolSpec, fence
from revos.mcp.scope import RunScope, ToolBlocked, as_uuid, require_own


def get_deal(ctx: ToolContext, scope: RunScope, args: dict[str, Any]) -> dict[str, Any]:
    require_own(as_uuid(args, "deal_id"), scope.deal_id, "deal_id")
    row = ctx.conn.cursor(row_factory=dict_row).execute(
        "SELECT stage, tier, fit_score, intent_score, engagement_score, total_score, score_reason, "
        "fields, segment_id FROM deals WHERE id = %s", (scope.deal_id,)).fetchone()
    if row is None:
        raise ToolBlocked("deal_id does not belong to this run")
    fields, withheld = {}, []
    for name, value in (row.pop("fields") or {}).items():
        if isinstance(value, str):     # call and reply values came from customers: fence them
            block = fence(ctx, scope, "transcript", f"deal-field-{name}", value)
            if block is None:
                withheld.append(name)
            else:
                fields[name] = block
        else:
            fields[name] = value
    return {"deal_id": str(scope.deal_id), **row, "fields": fields,
            "fields_withheld_for_review": withheld}


def get_contact(ctx: ToolContext, scope: RunScope, args: dict[str, Any]) -> dict[str, Any]:
    require_own(as_uuid(args, "contact_id"), scope.contact_id, "contact_id")
    row = ctx.conn.cursor(row_factory=dict_row).execute(
        "SELECT full_name, timezone, suppressed, split_part(email, '@', 2) AS email_domain "
        "FROM contacts WHERE id = %s", (scope.contact_id,)).fetchone()
    if row is None:
        raise ToolBlocked("contact_id does not belong to this run")
    return {"contact_id": str(scope.contact_id), **row}       # the address itself is not exposed


def get_company(ctx: ToolContext, scope: RunScope, args: dict[str, Any]) -> dict[str, Any]:
    require_own(as_uuid(args, "company_id"), scope.company_id, "company_id")
    row = ctx.conn.cursor(row_factory=dict_row).execute(
        "SELECT name, domain, data FROM companies WHERE id = %s", (scope.company_id,)).fetchone()
    if row is None:
        raise ToolBlocked("company_id does not belong to this run")
    return {"company_id": str(scope.company_id), **row}


SPECS = [
    ToolSpec("crm", "get_deal", "Read this run's deal: stage, tier, scores, reason, call fields.",
             {"deal_id": {"type": "string"}}, ("deal_id",), get_deal),
    ToolSpec("crm", "get_contact", "Read this run's contact (name, time zone; no email address).",
             {"contact_id": {"type": "string"}}, ("contact_id",), get_contact),
    ToolSpec("crm", "get_company", "Read this run's company record.",
             {"company_id": {"type": "string"}}, ("company_id",), get_company),
]
