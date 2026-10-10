from revos.agents.common import clean_fields, make
from revos.agents.types import AgentEnv, AgentOutput
from revos.skills.io import render_io, validate_io


def run(env: AgentEnv) -> AgentOutput:
    """Build the IO from deal data and the SAMPLE rate card, then validate it. A fixed procedure.

    A failing IO is stored as a draft and handed to a person with every reason; it is never
    proposed for signature. A discount over the maximum is proposed, so the policy engine blocks
    it and the block is on the record until a manager approves."""
    deal = env.tools.call("crm.get_deal", {"deal_id": str(env.deal_id)})
    company = env.tools.call("crm.get_company", {"company_id": str(env.company_id)})
    fields = clean_fields(deal["fields"])
    today = env.clock.now().date()
    validation = validate_io(env.pack, fields, today=today)
    document = render_io(env.pack, company["name"], validation, fields,
                         io_number=f"IO-{str(env.deal_id)[:8]}", date_=today.isoformat())
    d, c = env.deal_id, env.contact_id
    build = make("generate_io", d, c, {"document": document, "ok": validation.ok,
                                       "problems": list(validation.problems),
                                       "total": validation.total,
                                       "line_items": list(validation.line_items),
                                       "discount_pct": fields.get("discount_pct", 0) or 0},
                 [], "IO draft built from deal data")
    only_discount = validation.needs_manager_approval and len(validation.problems) == 1
    if not validation.ok and not only_discount:
        return AgentOutput((build,), handoff="IO validation failed: " + "; ".join(validation.problems))
    send = make("send_io", d, c, {
        "title": f"Insertion order for {company['name']}", "document": document,
        "signer_name": fields["signer_name"], "signer_role": fields["signer_role"],
        "total": validation.total, "discount_pct": fields.get("discount_pct", 0) or 0},
        [], "IO validated; ready for signature" if validation.ok
        else "discount above the maximum; needs a manager")
    return AgentOutput((build, send))
