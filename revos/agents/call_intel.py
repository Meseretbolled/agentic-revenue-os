from revos.agents.common import clean_fields, make
from revos.agents.types import AgentEnv, AgentOutput
from revos.skills.inbound import extract_call_fields
from revos.skills.people import parse_person_role, resolve_signer


def run(env: AgentEnv) -> AgentOutput:
    """Turn a call transcript into deal fields. Each value must come with a quote that really is in
    the transcript, and the quote is stored next to the value; anything else is dropped."""
    transcript = env.untrusted["transcript"]
    wanted = list(env.pack.stages.qualification_fields)
    result = extract_call_fields(env.judge, transcript, wanted)
    if not result.fields:
        return AgentOutput(needs_human="no call field could be backed by a quote from the transcript",
                           notes=str(result.rejected))
    properties: dict[str, object] = {}
    quotes: dict[str, str] = {}
    for name, item in result.fields.items():
        properties[name] = item.value
        properties[f"quote:{name}"] = item.quote
        quotes[name] = item.quote
    if "decision_maker" in result.fields:
        person = parse_person_role(result.fields["decision_maker"].quote)
        if person:                                      # both parts are words from that quote
            properties["decision_maker_name"] = person.name
            properties["decision_maker_role"] = person.role
            properties["quote:decision_maker_name"] = result.fields["decision_maker"].quote
    # The signer's name: the call's quote when it names one, else the lead record's signer. The signer's
    # ADDRESS is never read from a call (it is not an allowed field and the record alone supplies it).
    record = clean_fields(env.tools.call("crm.get_deal", {"deal_id": str(env.deal_id)})["fields"])
    signed = result.fields.get("signer")
    signer = resolve_signer(signed.quote if signed else None, record.get("signer_name"),
                            record.get("signer_role"))
    handoff = None
    if signer is None:
        handoff = "no signer name on the call or on the lead record: a person must say who signs"
    else:
        properties["signer_name"], properties["signer_role"] = signer.name, signer.role
        properties["signer_name_source"] = signer.source
        if signer.source == "call" and signed:
            properties["quote:signer_name"] = signed.quote
    proposal = make("enrich", env.deal_id, env.contact_id,
                    {"properties": properties, "quotes": quotes, "source": transcript.origin,
                     "rejected": dict(result.rejected)},
                    sorted(result.fields), f"{len(result.fields)} call field(s) with quotes")
    return AgentOutput((proposal,), notes=f"rejected: {result.rejected}" if result.rejected else "",
                       handoff=handoff)
