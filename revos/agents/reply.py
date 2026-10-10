from datetime import datetime

from revos.agents.common import first_name, make, match_tags
from revos.agents.types import AgentEnv, AgentOutput
from revos.domain.errors import QuarantinedContent
from revos.domain.stages import DealStage
from revos.skills.dates import return_date
from revos.skills.drafting import render_case_study_reply, render_fixed
from revos.skills.inbound import triage_reply
from revos.skills.slots import match_slot, slot_text

_STAGE_FOR = {"unsubscribe": DealStage.DO_NOT_CONTACT, "not_interested": DealStage.CLOSED_LOST,
              "out_of_office": None}


def run(env: AgentEnv) -> AgentOutput:
    """Classify a reply and propose what follows from the class. Nothing is done here."""
    reply = env.untrusted["reply"]
    triage = triage_reply(env.judge, reply)
    if triage.route == "quarantine":
        raise QuarantinedContent(triage.findings)
    if triage.route == "human":
        return AgentOutput(needs_human=triage.reason)

    label = triage.classification.label
    d, c = env.deal_id, env.contact_id
    message_id = env.input.get("message_id")
    proposals = [make("classify_reply", d, c, {
        "message_id": message_id, "label": label, "confidence": triage.classification.confidence,
        "rationale": triage.classification.rationale}, [], f"reply classified as {label}")]
    stage = _STAGE_FOR.get(label, DealStage.REPLIED)
    handoff = None

    def note(text: str):
        return make("crm_note", d, c, {"record_id": str(d), "text": text}, [], text)

    if label == "unsubscribe":
        proposals += [make("suppress_contact", d, c, {"reason": "unsubscribed by reply"}, [],
                           "the contact asked to stop"),
                      make("cancel_scheduled_sends", d, c, {}, [], "no more emails after an opt-out")]
    elif label == "not_interested":
        proposals.append(make("cancel_scheduled_sends", d, c, {}, [], "they are not interested"))
    elif label == "out_of_office":
        until = return_date(reply, env.clock.now().date())
        proposals.append(make("snooze_ooo", d, c, {
            "message_id": message_id, "until": until.isoformat() if until else None,
            "source": "stated" if until else "default"}, [], "auto-reply: wait, then follow up"))
    elif label == "price_objection":
        deal = env.tools.call("crm.get_deal", {"deal_id": str(d)})
        tags = match_tags(deal["fields"])
        picked = env.tools.call("casestudies.pick", {
            "tags": tags, "query": " ".join(tags), "situation": "price_objection"})["case_study"]
        study = next((s for s in env.pack.case_studies if picked and s.id == picked["id"]), None)
        if study is None:
            handoff = "price objection: no approved case study fits, a person should answer"
        else:
            contact = env.tools.call("crm.get_contact", {"contact_id": str(c)})
            draft = render_case_study_reply(env.pack, study, {
                "first_name": first_name(contact.get("full_name")),
                "sender_name": env.identity.sender_name,
                "unsubscribe_line": env.identity.unsubscribe_line,
                "postal_address": env.identity.postal_address},
                str(env.input.get("original_subject") or "your question"))
            proposals += [
                make("attach_case_study", d, c, {"case_study_id": study.id}, [study.id],
                     "proof for a price objection"),
                make("follow_up", d, c, {
                    "subject": draft.subject, "body": draft.body, "claims": [],
                    "case_study_id": study.id, "template": "case_study_reply",
                    "in_reply_to": message_id, "lesson_ids": [], "approved_extra": [],
                    "tier": deal.get("tier")}, [study.id],
                     f"answer the objection with {study.customer}'s stored facts")]
    elif label == "accept_slot":
        deal = env.tools.call("crm.get_deal", {"deal_id": str(d)})
        offered = [datetime.fromisoformat(s) for s in deal["fields"].get("offered_slots", [])]
        chosen = match_slot(reply, offered)
        if chosen is None:
            handoff = "accept_slot: the reply does not match one of the times we offered"
        else:
            contact = env.tools.call("crm.get_contact", {"contact_id": str(c)})
            text = slot_text(chosen)
            draft = render_fixed(env.pack, "meeting_confirmation", {
                "first_name": first_name(contact.get("full_name")), "slot_text": text,
                "sender_name": env.identity.sender_name,
                "unsubscribe_line": env.identity.unsubscribe_line,
                "postal_address": env.identity.postal_address})
            invite = {"start": chosen.isoformat(), "minutes": 30,
                      "summary": f"Intro call with {contact.get('full_name') or 'you'}"}
            proposals += [
                make("book_meeting", d, c, {"start": chosen.isoformat(), "title": invite["summary"],
                                            "message_id": message_id}, [], "they accepted an offered time"),
                make("follow_up", d, c, {
                    "subject": draft.subject, "body": draft.body, "claims": [],
                    "template": "meeting_confirmation", "invite": invite, "in_reply_to": message_id,
                    "case_study_id": None, "lesson_ids": [], "approved_extra": [text],
                    "tier": deal.get("tier")}, [], "confirm the time, with the invite attached")]
    elif label == "question":
        proposals.append(note(f"Prospect asked a question (message {message_id}); a person should answer."))
        handoff = "question: a person should answer"
    elif label == "referral":
        proposals.append(note(f"Prospect suggested someone else (message {message_id}); review."))
        handoff = "referral: a person decides who to contact"
    return AgentOutput(tuple(proposals), stage_suggestion=stage, handoff=handoff)

