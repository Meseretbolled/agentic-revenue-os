from revos.agents.common import make, match_tags
from revos.agents.types import AgentEnv, AgentOutput
from revos.domain.errors import QuarantinedContent
from revos.domain.stages import DealStage
from revos.skills.inbound import triage_reply

_STAGE_FOR = {"unsubscribe": DealStage.DO_NOT_CONTACT, "not_interested": DealStage.CLOSED_LOST}


def run(env: AgentEnv) -> AgentOutput:
    reply = env.untrusted["reply"]
    triage = triage_reply(env.judge, reply)
    if triage.route == "quarantine":
        raise QuarantinedContent(triage.findings)
    if triage.route == "human":
        return AgentOutput(needs_human=triage.reason)

    label = triage.classification.label
    d, c = env.deal_id, env.contact_id
    proposals = [make("classify_reply", d, c, {
        "message_id": env.input.get("message_id"), "label": label,
        "confidence": triage.classification.confidence,
        "rationale": triage.classification.rationale}, [], f"reply classified as {label}")]
    if label == "unsubscribe":
        proposals += [make("suppress_contact", d, c, {"reason": "unsubscribed by reply"}, [],
                           "the contact asked to stop"),
                      make("cancel_scheduled_sends", d, c, {}, [], "no more emails after an opt-out")]
    elif label == "price_objection":
        deal = env.tools.call("crm.get_deal", {"deal_id": str(d)})
        tags = match_tags(deal["fields"])
        picked = env.tools.call("casestudies.pick", {"tags": tags, "query": " ".join(tags)})
        if picked["case_study"]:
            proposals.append(make("attach_case_study", d, c, {"case_study_id": picked["case_study"]["id"]},
                                  [picked["case_study"]["id"]], "proof for a price objection"))
    elif label == "out_of_office":
        proposals.append(make("snooze_ooo", d, c, {}, [], "auto-reply: wait before the next step"))
    return AgentOutput(tuple(proposals), stage_suggestion=_STAGE_FOR.get(label, DealStage.REPLIED))
