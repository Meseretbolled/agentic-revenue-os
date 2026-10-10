from revos.agents.common import clean_fields, make
from revos.agents.types import AgentEnv, AgentOutput
from revos.domain.stages import DealStage


def run(env: AgentEnv) -> AgentOutput:
    """Check the pack's exit criteria for 'qualified' against the deal. A fixed procedure.

    It only suggests the move. The state machine checks the same criteria again before moving."""
    deal = env.tools.call("crm.get_deal", {"deal_id": str(env.deal_id)})
    fields = clean_fields(deal["fields"])
    required = env.pack.stages.exit_criteria.get(DealStage.QUALIFIED, [])
    missing = [name for name in required if not fields.get(name)]
    if missing:
        note = make("crm_note", env.deal_id, env.contact_id, {
            "record_id": str(env.deal_id), "text": "Qualification hold. Missing: " + ", ".join(missing)},
            missing, "exit criteria not met")
        return AgentOutput((note,), notes="held: " + ", ".join(missing))
    note = make("crm_note", env.deal_id, env.contact_id, {
        "record_id": str(env.deal_id), "text": "Qualified: all exit criteria are met."},
        required, "exit criteria met")
    return AgentOutput((note,), stage_suggestion=DealStage.QUALIFIED)
