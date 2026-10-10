from dataclasses import dataclass
from typing import Any

from revos.agents.common import (
    check_inputs,
    clean_fields,
    evidence_from_fields,
    first_name,
    lesson_query,
    make,
    match_tags,
)
from revos.agents.types import AgentEnv, AgentOutput
from revos.domain.drafting import Evidence
from revos.ports.llm import DraftRequest
from revos.segments.schema import CaseStudy
from revos.skills.angle import choose_pillar
from revos.skills.drafting import DraftOutcome, draft_with_checks


@dataclass(frozen=True)
class Drafted:
    outcome: DraftOutcome
    study: CaseStudy | None
    lessons: list[dict[str, Any]]          # everything retrieved
    shaping: list[int]                     # the ids that actually chose the angle
    angle: str
    deal: dict[str, Any]
    evidence: Evidence


def draft_and_check(env: AgentEnv, template_name: str, *, use_case_study: bool = True,
                    extra_context=None, extra_approved=()) -> Drafted:
    """Gather context through scoped tools, let the lessons choose the angle, draft with the model,
    check with code. Shared with the meeting agent, which writes a different email the same way."""
    tools = env.tools
    deal = tools.call("crm.get_deal", {"deal_id": str(env.deal_id)})
    contact = tools.call("crm.get_contact", {"contact_id": str(env.contact_id)})
    company = tools.call("crm.get_company", {"company_id": str(env.company_id)})
    tags = match_tags(deal["fields"])

    picked = (tools.call("casestudies.pick", {"tags": tags, "query": " ".join(tags),
                                              "situation": template_name})["case_study"]
              if use_case_study else None)
    study = next((c for c in env.pack.case_studies if picked and c.id == picked["id"]), None)
    keywords, lesson_tags = lesson_query(deal["fields"])
    lessons = tools.call("lessons.search", {"query": keywords, "tags": lesson_tags, "limit": 3})["lessons"]
    pillar, shaping = choose_pillar(env.pack.messaging.pillars, lessons)

    template = env.pack.messaging.templates[template_name]
    evidence = evidence_from_fields(deal["fields"])
    context = {"company": company["name"], "first_name": first_name(contact.get("full_name")),
               "sender_name": env.identity.sender_name,
               "unsubscribe_line": env.identity.unsubscribe_line,
               "postal_address": env.identity.postal_address,
               "pillar": pillar, "case_study": study, "angle": pillar.headline,
               **(extra_context or {})}
    approved, names = check_inputs(env.pack, template, env.identity, company["name"],
                                   contact.get("full_name"), study, extra_approved)
    request = DraftRequest(template.subject, template.body, context, evidence)
    outcome = draft_with_checks(env.judge, request, approved, names, env.verifier)
    return Drafted(outcome, study, lessons, shaping, pillar.id, deal, evidence)


def run(env: AgentEnv) -> AgentOutput:
    kind = env.input.get("kind", "first_touch")
    template = "first_touch" if kind == "first_touch" else "follow_up"
    drafted = draft_and_check(env, template)
    outcome = drafted.outcome
    if outcome.error is not None:
        raise outcome.error                    # a typed stop: the orchestrator records its cause
    if outcome.status != "passed":
        codes = ", ".join(sorted({i.code for i in outcome.issues}))
        return AgentOutput(needs_human=f"draft did not pass the checks after {outcome.attempts} "
                                       f"attempt(s): {codes}",
                           notes="; ".join(i.message for i in outcome.issues)[:1000])
    draft = outcome.draft
    cited = sorted({e for c in draft.claims for e in c.evidence})
    by_id = {x["id"]: x for x in drafted.lessons}
    payload = {"subject": draft.subject, "body": draft.body,
               "claims": [{"text": c.text, "evidence": list(c.evidence)} for c in draft.claims],
               "case_study_id": drafted.study.id if drafted.study else None,
               "lesson_ids": drafted.shaping, "angle": drafted.angle,
               "citations": {"lessons": [{"id": i, "text": by_id.get(i, {}).get("text", ""),
                                          "confidence": by_id.get(i, {}).get("confidence", 0)}
                                         for i in drafted.shaping]},
               "attempts": outcome.attempts, "tier": clean_fields(drafted.deal)["tier"],
               "template": template, "approved_extra": []}
    return AgentOutput((make(kind, env.deal_id, env.contact_id, payload, cited,
                             f"{kind.replace('_', ' ')} drafted from {len(cited)} cited fact(s)"),))
