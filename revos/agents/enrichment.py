from revos.agents.common import DEMO_FACT_PREFIX, DEMO_SOURCE_PREFIX, make, unfence
from revos.agents.types import AgentEnv, AgentOutput
from revos.skills.extraction import extract_fields


def run(env: AgentEnv) -> AgentOutput:
    """Read the company's own site and record what it says, each value with its sentence."""
    deal_id = str(env.deal_id)
    tools = env.tools
    tools.call("crm.get_deal", {"deal_id": deal_id})
    company = tools.call("crm.get_company", {"company_id": str(env.company_id)})
    domain = company.get("domain")
    if not domain:
        return AgentOutput(needs_human="the company has no website domain to research")

    found: dict[str, tuple[object, str, str]] = {}
    withheld: list[str] = []
    for path in env.pack.enrichment.pages:
        url = f"https://{domain}{path}"
        page = tools.call("research.fetch_site", {"url": url})
        if not page.get("found"):
            continue
        if page.get("content") is None:
            withheld.append(url)                       # quarantined by the tool; a person reviews it
            continue
        for item in extract_fields(unfence(page["content"]), env.pack.enrichment.extractors):
            found.setdefault(item.field, (item.value, item.quote, url))

    properties: dict[str, object] = {"company_domain": domain}
    fact_ids: list[str] = []
    quote_ids: dict[str, str] = {}
    for field, (value, quote, url) in found.items():
        properties[field] = value
        if quote not in quote_ids:
            fact_id = f"F{len(quote_ids) + 1}"
            quote_ids[quote] = fact_id
            properties[f"fact:{fact_id}"] = quote
            properties[f"fact_source:{fact_id}"] = url
            fact_ids.append(fact_id)
    demo_notes = _demo_notes(env, properties, fact_ids)
    reason = f"found {len(found)} field(s) on {domain}"
    if demo_notes:
        reason += (f"; stored {demo_notes} sourced fact(s) from the seed profile "
                   "(fictional demo data, NOT real research)")
    proposal = make("enrich", env.deal_id, env.contact_id, {"properties": properties}, fact_ids, reason)
    note = f"{len(withheld)} page(s) withheld for review" if withheld else ""
    return AgentOutput((proposal,), notes=note)


def _demo_notes(env: AgentEnv, properties: dict[str, object], fact_ids: list[str]) -> int:
    """Facts that came with a fictional demo profile are stored as sourced facts too, after any real
    research (which therefore comes first), and always labelled as fictional demo data."""
    deal = env.tools.call("crm.get_deal", {"deal_id": str(env.deal_id)})
    notes = deal["fields"].get("research_notes") or []
    stored = 0
    for item in notes if isinstance(notes, list) else []:
        text = str(item.get("text", "")).strip() if isinstance(item, dict) else ""
        if not text:
            continue
        stored += 1
        fact_id = f"{DEMO_FACT_PREFIX}{stored}"
        label = str(item.get("source", "")).strip()
        if not label.lower().startswith(DEMO_SOURCE_PREFIX):
            label = f"{DEMO_SOURCE_PREFIX}: {label}".rstrip(": ")
        properties[f"fact:{fact_id}"] = text
        properties[f"fact_source:{fact_id}"] = label
        fact_ids.append(fact_id)
    return stored
