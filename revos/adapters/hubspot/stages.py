"""Our stage names onto HubSpot's default deal pipeline. The exact stage is always kept as well in
the custom property revos_stage, because several of ours share one HubSpot stage."""

from collections.abc import Mapping

from revos.domain.stages import DealStage

PIPELINE = "default"

DEFAULT_STAGE_MAP: Mapping[DealStage, str] = {
    DealStage.NEW: "appointmentscheduled",
    DealStage.ENRICHED: "appointmentscheduled",
    DealStage.SCORED: "appointmentscheduled",
    DealStage.OUTREACH: "appointmentscheduled",
    DealStage.REPLIED: "appointmentscheduled",
    DealStage.MEETING: "presentationscheduled",
    DealStage.QUALIFIED: "qualifiedtobuy",
    DealStage.PROPOSAL: "decisionmakerboughtin",
    DealStage.IO_APPROVAL: "decisionmakerboughtin",
    DealStage.OUT_FOR_SIGNATURE: "contractsent",
    DealStage.CLOSED_WON: "closedwon",
    DealStage.CLOSED_LOST: "closedlost",
    DealStage.HANDOFF: "closedwon",
    DealStage.DO_NOT_CONTACT: "closedlost",
}


def hubspot_stage(stage: DealStage | str, mapping: Mapping[DealStage, str] = DEFAULT_STAGE_MAP) -> str:
    return mapping[DealStage(stage)]
