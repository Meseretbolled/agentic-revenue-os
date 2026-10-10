from datetime import datetime

from revos.agents.common import clean_fields, make
from revos.agents.types import AgentEnv, AgentOutput
from revos.skills.io import render_io, validate_io


def run(env: AgentEnv) -> AgentOutput:
    """Build the IO from deal data and the SAMPLE rate card, then validate it. A fixed procedure."""
    deal = env.tools.call("crm.get_deal", {"deal_id": str(env.deal_id)})
    company = env.tools.call("crm.get_company", {"company_id": str(env.company_id)})
    contact = env.tools.call("crm.get_contact", {"contact_id": str(env.contact_id)})
    fields = clean_fields(deal["fields"])
    validation = validate_io(env.pack, fields)
    discount = float(fields.get("discount_pct", 0) or 0)
    document = render_io(env.pack, company["name"], validation, discount,
                         io_number=f"IO-{str(env.deal_id)[:8]}",
                         date=datetime.fromtimestamp(env.clock.now().timestamp()).date().isoformat())
    d, c = env.deal_id, env.contact_id
    build = make("generate_io", d, c, {
        "document": document, "ok": validation.ok, "problems": list(validation.problems),
        "total": validation.total, "discount_pct": discount}, [], "IO draft built from deal data")
    if not validation.ok:
        return AgentOutput((build,), needs_human="IO validation failed: " + "; ".join(validation.problems))
    send = make("send_io", d, c, {
        "title": f"Insertion order for {company['name']}", "document": document,
        "signer_name": contact.get("full_name") or "", "total": validation.total,
        "discount_pct": discount}, [], "IO validated; ready for signature")
    return AgentOutput((build, send))
