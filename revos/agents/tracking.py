from revos.agents.common import make
from revos.agents.types import AgentEnv, AgentOutput
from revos.domain.stages import DealStage


def run(env: AgentEnv) -> AgentOutput:
    """Flag a deal that has sat in its stage longer than the pack's SLA. A fixed procedure."""
    stage = DealStage(env.input["stage"])
    hours = float(env.input["hours_in_stage"])
    sla = env.pack.stages.sla_hours.get(stage)
    env.tools.call("crm.get_deal", {"deal_id": str(env.deal_id)})
    if sla is None or hours <= sla:
        return AgentOutput(notes=f"within SLA ({hours:g}h of {sla}h)" if sla else "no SLA for stage")
    reminder = make("reminder", env.deal_id, env.contact_id, {
        "stage": stage.value, "hours_in_stage": hours, "sla_hours": sla},
        [], f"stalled in {stage.value}: {hours:g}h against an SLA of {sla}h")
    return AgentOutput((reminder,))
