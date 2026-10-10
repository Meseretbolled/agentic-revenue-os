from revos.agents.common import clean_fields, make
from revos.agents.outreach import draft_and_check
from revos.agents.types import AgentEnv, AgentOutput
from revos.skills.brief import build_brief


def run(env: AgentEnv) -> AgentOutput:
    """Offer meeting times and write a call brief that cites the lessons it used."""
    if not env.slots:
        return AgentOutput(needs_human="no free calendar slots to offer")
    slots_text = "\n".join(f"- {s.start:%A %d %B %H:%M} UTC" for s in env.slots[:3])
    drafted = draft_and_check(env, "meeting_offer", use_case_study=False,
                              extra_context={"slots_text": slots_text}, extra_approved=[slots_text])
    outcome = drafted.outcome
    if outcome.error is not None:
        raise outcome.error
    if outcome.status != "passed":
        return AgentOutput(needs_human="meeting offer did not pass the checks: "
                           + ", ".join(sorted({i.code for i in outcome.issues})))
    draft = outcome.draft
    cited = sorted({e for c in draft.claims for e in c.evidence})
    offer = make("follow_up", env.deal_id, env.contact_id, {
        "subject": draft.subject, "body": draft.body,
        "claims": [{"text": c.text, "evidence": list(c.evidence)} for c in draft.claims],
        "case_study_id": None, "lesson_ids": [], "citations": {"lessons": []},
        "attempts": outcome.attempts, "template": "meeting_offer", "approved_extra": [slots_text]},
        cited, "meeting times offered")

    company = env.tools.call("crm.get_company", {"company_id": str(env.company_id)})
    contact = env.tools.call("crm.get_contact", {"contact_id": str(env.contact_id)})
    deal = drafted.deal
    lessons = drafted.lessons
    brief = build_brief(company=company["name"], contact=contact.get("full_name"),
                        stage=deal["stage"], tier=deal["tier"], total=deal["total_score"],
                        fields=clean_fields(deal["fields"]), evidence=drafted.evidence,
                        lessons=lessons)
    note = make("crm_note", env.deal_id, env.contact_id, {
        "record_id": str(env.deal_id), "text": brief, "lesson_ids": [x["id"] for x in lessons],
        "citations": {"lessons": [{"id": x["id"], "text": x["text"], "confidence": x["confidence"]}
                                  for x in lessons]}},
        [*cited, *(f"L{x['id']}" for x in lessons)], "call brief citing the lessons it used")
    return AgentOutput((offer, note))
