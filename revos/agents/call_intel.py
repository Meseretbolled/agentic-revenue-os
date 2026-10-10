from revos.agents.common import make
from revos.agents.types import AgentEnv, AgentOutput
from revos.skills.inbound import extract_call_fields
from revos.skills.people import parse_person_role


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
    person = parse_person_role(result.fields["decision_maker"].quote) \
        if "decision_maker" in result.fields else None
    if person:                      # both parts are words from the decision-maker quote
        properties["decision_maker_name"] = person.name
        properties["decision_maker_role"] = person.role
        properties["quote:decision_maker_name"] = result.fields["decision_maker"].quote
    proposal = make("enrich", env.deal_id, env.contact_id,
                    {"properties": properties, "quotes": quotes, "source": transcript.origin},
                    sorted(result.fields), f"{len(result.fields)} call field(s) with quotes")
    return AgentOutput((proposal,), notes=f"rejected: {result.rejected}" if result.rejected else "")
