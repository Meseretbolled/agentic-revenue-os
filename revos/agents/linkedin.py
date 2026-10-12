"""Draft a LinkedIn connection note and follow-up message. DRAFTS only: this agent has no way to reach
LinkedIn (its tools are read-only lookups), and nothing in the system sends what it proposes."""

from revos.agents.common import clean_fields, evidence_from_fields, make, match_tags
from revos.agents.types import AgentEnv, AgentOutput
from revos.skills import linkedin
from revos.skills.drafting import draft_with_fallback


def run(env: AgentEnv) -> AgentOutput:
    cfg = env.pack.messaging.linkedin
    if cfg is None:
        return AgentOutput(needs_human="this segment's pack has no linkedin section")
    tools = env.tools
    deal = tools.call("crm.get_deal", {"deal_id": str(env.deal_id)})
    contact = tools.call("crm.get_contact", {"contact_id": str(env.contact_id)})
    company = tools.call("crm.get_company", {"company_id": str(env.company_id)})
    study = None
    if cfg.use_case_study:
        tags = [*match_tags(deal["fields"]), "general"]
        picked = tools.call("casestudies.pick", {"tags": tags, "query": " ".join(tags),
                                                 "situation": "linkedin"})["case_study"]
        study = next((c for c in env.pack.case_studies if picked and c.id == picked["id"]), None)
    evidence = evidence_from_fields(deal["fields"])
    if not evidence.items:
        return AgentOutput(needs_human="no researched facts for this lead: nothing to base a draft on")
    sender = env.identity.sender_name
    context = linkedin.build_context(company["name"], contact.get("full_name"), sender, study, evidence)
    approved, names = linkedin.inputs(env.pack, sender, company["name"], contact.get("full_name"), study)

    pieces: dict[str, dict] = {}
    fell_back: dict[str, list[str]] = {}
    attempts = 0
    for piece in linkedin.pieces(cfg):
        request = linkedin.request_for(piece, cfg, context, evidence)
        outcome = draft_with_fallback(env.judge, request, approved, names, env.verifier, piece.fallback)
        if outcome.error is not None:
            raise outcome.error                        # a typed stop: the orchestrator records its cause
        if outcome.status != "passed" or outcome.draft is None:
            codes = ", ".join(sorted({i.code for i in outcome.issues}))
            return AgentOutput(needs_human=f"the LinkedIn {piece.kind} did not pass the checks: {codes}",
                               notes="; ".join(i.message for i in outcome.issues)[:1000])
        attempts += outcome.attempts
        pieces[piece.kind] = {"text": outcome.draft.body,
                              "claims": [{"text": c.text, "evidence": list(c.evidence)}
                                         for c in outcome.draft.claims]}
        if outcome.fallback:
            fell_back[piece.kind] = list(outcome.fallback)
    cited = sorted({e for p in pieces.values() for c in p["claims"] for e in c["evidence"]})
    payload = {"pieces": pieces, "channel": "linkedin", "draft_only": True,
               "body": "\n\n".join(f"{k.upper()}:\n{pieces[k]['text']}" for k in pieces),
               "claims": [c for p in pieces.values() for c in p["claims"]],
               "case_study_id": study.id if study else None, "attempts": attempts,
               "tier": clean_fields(deal)["tier"],
               "draft_source": "template" if len(fell_back) == len(pieces) else "model",
               **({"fallback": {"checks_failed": sorted({c for v in fell_back.values() for c in v}),
                                "attempts": attempts, "pieces": sorted(fell_back)}} if fell_back else {})}
    return AgentOutput((make("linkedin_draft", env.deal_id, env.contact_id, payload, cited,
                             f"LinkedIn note and message drafted from {len(cited)} cited fact(s)"),))
