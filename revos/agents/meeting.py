from revos.agents.common import make
from revos.agents.outreach import draft_and_check
from revos.agents.types import AgentEnv, AgentOutput


def run(env: AgentEnv) -> AgentOutput:
    """Offer meeting times and write a call brief that cites the lessons it used."""
    if not env.slots:
        return AgentOutput(needs_human="no free calendar slots to offer")
    slots_text = "\n".join(f"- {s.start:%A %d %B %H:%M} UTC" for s in env.slots[:3])
    outcome, _study, lesson_ids, _deal, evidence = draft_and_check(
        env, "meeting_offer", use_case_study=False, extra_context={"slots_text": slots_text},
        extra_approved=[slots_text])
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
        "case_study_id": None, "lesson_ids": lesson_ids, "attempts": outcome.attempts,
        "template": "meeting_offer", "approved_extra": [slots_text]},
        cited, "meeting times offered")
    facts = "; ".join(f"[{i.id}] {i.text}" for i in evidence.items.values()) or "none recorded"
    lessons = ", ".join(f"L{i}" for i in lesson_ids) or "none"
    brief = make("crm_note", env.deal_id, env.contact_id, {
        "record_id": str(env.deal_id),
        "text": f"Call brief. Facts: {facts}. Lessons used: {lessons}."},
        [*cited, *(f"L{i}" for i in lesson_ids)], "call brief citing the lessons used")
    return AgentOutput((offer, brief))
