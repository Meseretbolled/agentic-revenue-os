from revos.agents.common import clean_fields, make
from revos.agents.outreach import draft_and_check
from revos.agents.types import AgentEnv, AgentOutput
from revos.skills.brief import build_brief
from revos.skills.slots import resolve_tz, slot_label


def run(env: AgentEnv) -> AgentOutput:
    """Offer meeting times and write a call brief that cites the lessons it used."""
    if not env.slots:
        return AgentOutput(needs_human="no free calendar slots to offer")
    contact_row = env.tools.call("crm.get_contact", {"contact_id": str(env.contact_id)})
    tz = resolve_tz(contact_row.get("timezone"))
    slots_text = "\n".join(f"- {slot_label(s.start, tz)}" for s in env.slots[:3])
    snippets = env.pack.messaging.snippets
    label = str(env.input.get("reply_label") or "interested")
    ack = snippets.get(f"reply_ack_{label}") or snippets["reply_ack_default"]
    intro = snippets["slots_intro_local" if tz else "slots_intro_utc"]
    drafted = draft_and_check(
        env, "meeting_offer", use_case_study=False, purpose="reply_ack", fallback_line=ack,
        extra_context={"slots_text": slots_text, "slots_intro": intro, "reply_ack": ack},
        extra_approved=[slots_text, ack, intro])
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
        "slots": [s.start.isoformat() for s in env.slots[:3]],            # always UTC
        "timezone": getattr(tz, "key", None), "slots_shown": slots_text.splitlines(),
        "attempts": outcome.attempts, "template": "meeting_offer",
        "approved_extra": [slots_text, ack, intro],
        "draft_source": "template" if outcome.fallback else "model",
        **({"fallback": {"checks_failed": list(outcome.fallback), "attempts": outcome.attempts}}
           if outcome.fallback else {})},
        cited, "meeting times offered")

    company = env.tools.call("crm.get_company", {"company_id": str(env.company_id)})
    contact = contact_row
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
