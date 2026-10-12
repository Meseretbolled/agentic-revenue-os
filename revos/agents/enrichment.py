from revos.agents.common import (
    DEMO_FACT_PREFIX,
    DEMO_SOURCE_PREFIX,
    WEB_FACT_PREFIX,
    clean_fields,
    make,
    unfence,
)
from revos.agents.types import AgentEnv, AgentOutput
from revos.skills.extraction import extract_fields
from revos.skills.web_research import HitView, PageView, run_web_enrichment


def run(env: AgentEnv) -> AgentOutput:
    """Read the company's own site and record what it says, each value with its sentence.
    With `enrichment_mode: tavily` in the run's input, search the web first (see `_run_web`)."""
    if env.input.get("enrichment_mode") == "tavily":
        return _run_web(env)
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


def _run_web(env: AgentEnv) -> AgentOutput:
    """Real web research for one lead. The model plays no part: every result goes through the scoped, fenced
    and scanned tools, then through the entity match (domain, name, location), and only accepted text is read
    by the pack's extractors. Rejections and any fallback to the offline behaviour go back for the audit."""
    tools = env.tools
    deal = tools.call("crm.get_deal", {"deal_id": str(env.deal_id)})
    company = tools.call("crm.get_company", {"company_id": str(env.company_id)})
    domain = company.get("domain")
    if not domain:
        return AgentOutput(needs_human="the company has no website domain to research")
    location = str(clean_fields(deal["fields"]).get("hq_location") or "").strip() or None
    held = 0

    def search(query: str) -> list[HitView]:
        nonlocal held
        result = tools.call("research.search", {"query": query})
        held += int(result.get("held_for_review", 0))
        return [HitView(r.get("title", ""), r["url"], unfence(r["snippet"]), r.get("fetched_at", ""),
                        r.get("origin", ""), r.get("note", "")) for r in result["results"]]

    def fetch(url: str) -> PageView | None:
        page = tools.call("research.fetch_site", {"url": url})
        if not page.get("found"):
            return None
        if page.get("content") is None:
            return PageView(url, None)
        return PageView(url, unfence(page["content"]), page.get("fetched_at", ""), page.get("origin", ""))

    outcome = run_web_enrichment(search=search, fetch=fetch, company=company["name"], domain=domain,
                                 location=location, web=env.pack.enrichment.web,
                                 extractors=env.pack.enrichment.extractors)
    properties: dict[str, object] = {"company_domain": domain}
    fact_ids: list[str] = []
    ids_by_quote: dict[str, str] = {}
    for item in outcome.facts:
        properties[item.field] = item.value
        if item.quote not in ids_by_quote:
            fact_id = f"{WEB_FACT_PREFIX}{len(ids_by_quote) + 1}"
            ids_by_quote[item.quote] = fact_id
            properties[f"fact:{fact_id}"] = item.quote
            properties[f"fact_source:{fact_id}"] = item.url
            properties[f"fact_fetched:{fact_id}"] = item.fetched_at
            fact_ids.append(fact_id)
    demo_notes = _demo_notes(env, properties, fact_ids)
    reason = (f"web research found {len(outcome.facts)} field(s) from {len(outcome.accepted_urls)} accepted "
              f"page(s) on {domain}; {len(outcome.rejected)} result(s) rejected")
    if outcome.fell_back:
        reason += f"; fell back to the offline behaviour ({outcome.fallback_reason})"
    if demo_notes:
        reason += f"; stored {demo_notes} sourced fact(s) from the seed profile (fictional demo data)"
    payload = {"properties": properties,
               "rejected_results": [{"url": r.url, "reasons": list(r.reasons), "origin": r.origin}
                                    for r in outcome.rejected[:50]],
               "enrichment": {"mode": "tavily", "calls": outcome.calls, "accepted": outcome.accepted_urls,
                              "withheld": outcome.withheld, "held_for_review": held,
                              "fell_back_to_mock": outcome.fell_back,
                              "fallback_reason": outcome.fallback_reason}}
    return AgentOutput((make("enrich", env.deal_id, env.contact_id, payload, fact_ids, reason),))
