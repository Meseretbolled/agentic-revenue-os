from collections.abc import Mapping, Sequence
from enum import StrEnum


class DealStage(StrEnum):
    NEW = "new"
    ENRICHED = "enriched"
    SCORED = "scored"
    OUTREACH = "outreach"
    REPLIED = "replied"
    MEETING = "meeting"
    QUALIFIED = "qualified"
    PROPOSAL = "proposal"
    IO_APPROVAL = "io_approval"
    OUT_FOR_SIGNATURE = "out_for_signature"
    CLOSED_WON = "closed_won"
    CLOSED_LOST = "closed_lost"
    HANDOFF = "handoff"
    DO_NOT_CONTACT = "do_not_contact"


S = DealStage

# The happy path, plus the few deliberate loops (follow-ups, IO revision).
_FORWARD: dict[DealStage, set[DealStage]] = {
    S.NEW: {S.ENRICHED},
    S.ENRICHED: {S.SCORED},
    S.SCORED: {S.OUTREACH},
    S.OUTREACH: {S.REPLIED},
    S.REPLIED: {S.OUTREACH, S.MEETING},
    S.MEETING: {S.QUALIFIED},
    S.QUALIFIED: {S.PROPOSAL},
    S.PROPOSAL: {S.IO_APPROVAL},
    S.IO_APPROVAL: {S.PROPOSAL, S.OUT_FOR_SIGNATURE},
    S.OUT_FOR_SIGNATURE: {S.CLOSED_WON},
    S.CLOSED_WON: {S.HANDOFF},
    S.CLOSED_LOST: {S.HANDOFF},
}

# Stages with no way out except the ones listed above.
TERMINAL: frozenset[DealStage] = frozenset({S.HANDOFF, S.DO_NOT_CONTACT})
_CLOSED: frozenset[DealStage] = frozenset({S.CLOSED_WON, S.CLOSED_LOST})

# Any live deal can be lost or marked do-not-contact (opt-out, bounce, disqualified).
ALLOWED_TRANSITIONS: Mapping[DealStage, frozenset[DealStage]] = {
    stage: frozenset(
        _FORWARD.get(stage, set())
        | (set() if stage in TERMINAL or stage in _CLOSED else {S.CLOSED_LOST, S.DO_NOT_CONTACT})
    )
    for stage in DealStage
}

# Exit criteria come from the segment pack: stage entered -> required field names.
ExitCriteria = Mapping[DealStage, Sequence[str]]


def can_transition(src: DealStage, dst: DealStage) -> bool:
    return dst in ALLOWED_TRANSITIONS[src]
