from revos.agents.common import lead_facts, make
from revos.agents.types import AgentEnv, AgentOutput
from revos.skills.scoring import score_lead


def run(env: AgentEnv) -> AgentOutput:
    """A fixed procedure: the pack's rules over the deal's recorded facts. No model."""
    deal = env.tools.call("crm.get_deal", {"deal_id": str(env.deal_id)})
    result = score_lead(lead_facts(deal["fields"], env.pack), env.pack.scoring)
    payload = {"fit": result.fit, "intent": result.intent, "engagement": result.engagement,
               "total": result.total, "tier": result.tier.value, "summary": result.summary}
    return AgentOutput((make("score", env.deal_id, env.contact_id, payload,
                             [r.rule_id for r in result.reasons if r.points],
                             f"tier {result.tier.value} ({result.total}/100)"),))
