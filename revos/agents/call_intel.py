from revos.agents.common import make
from revos.agents.types import AgentEnv, AgentOutput
from revos.skills.inbound import extract_call_fields


def run(env: AgentEnv) -> AgentOutput:
    """Turn a call transcript into deal fields. Each value must come with a quote that really is
    in the transcript; anything else is dropped."""
    transcript = env.untrusted["transcript"]
    wanted = list(env.pack.stages.qualification_fields)
    result = extract_call_fields(env.judge, transcript, wanted)
    if not result.fields:
        return AgentOutput(needs_human="no call field could be backed by a quote from the transcript",
                           notes=str(result.rejected))
    proposal = make("enrich", env.deal_id, env.contact_id,
                    {"properties": {name: f.value for name, f in result.fields.items()},
                     "quotes": {name: f.quote for name, f in result.fields.items()}},
                    sorted(result.fields), f"{len(result.fields)} call field(s) with quotes")
    return AgentOutput((proposal,), notes=f"rejected: {result.rejected}" if result.rejected else "")
