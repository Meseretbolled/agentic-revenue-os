"""The autonomy decision. Pure: no database, no clock. Allowlist first, hard blocks next."""

import re
from dataclasses import dataclass

from revos.domain.enums import AutonomyLevel, AutonomyMode, Outcome, Tier
from revos.policy.config import (
    DRAFT_ONLY_TYPES,
    EMAIL_BODY_TYPES,
    OUTBOUND_TYPES,
    PRICE_RULE_KEY,
    PolicyConfig,
)

# Content check for the "mentions price" rule. Keyword based on purpose: it is predictable and
# errs towards asking. It does not treat "$15.52 ROAS" in a case study as a price.
_PRICE_WORDS = re.compile(
    r"\b(price|prices|pricing|priced|cost|costs|fee|fees|discount|discounts|rate card|rates?|"
    r"cpm|cpc|quote|quoted)\b",
    re.IGNORECASE,
)

_RANK = {AutonomyLevel.AUTO: 0, AutonomyLevel.AUTO_WITH_VETO: 1, AutonomyLevel.NEEDS_APPROVAL: 2}
_OUTCOME = {
    AutonomyLevel.AUTO: Outcome.AUTO,
    AutonomyLevel.AUTO_WITH_VETO: Outcome.AUTO_WITH_VETO,
    AutonomyLevel.NEEDS_APPROVAL: Outcome.NEEDS_APPROVAL,
}


def mentions_price(text: str) -> bool:
    return bool(_PRICE_WORDS.search(text))


@dataclass(frozen=True)
class ActionRequest:
    """What the proposing agent/skill tells the engine about one action."""

    action_type: str
    tier: Tier | None = None
    body: str = ""
    claim_check_passed: bool | None = None     # None = not run, treated as failed
    injection_flagged: bool = False
    contact_suppressed: bool = False           # the service overwrites this from the database
    discount_pct: float | None = None
    rate_card_ok: bool | None = None           # from the rate-card skill; None = unknown
    from_library: bool = True                  # case study comes from the curated library
    io_validated: bool | None = None


@dataclass(frozen=True)
class PolicyDecision:
    outcome: Outcome
    level: AutonomyLevel | None
    reasons: tuple[str, ...]
    held: bool = False        # kill switch on: even an approved outbound action waits


def _allowed_types(config: PolicyConfig) -> set[str]:
    return {key.split(".")[0] for key in config.levels if key != PRICE_RULE_KEY}


def decide(
    request: ActionRequest,
    config: PolicyConfig,
    *,
    mode: AutonomyMode,
    kill_switch: bool,
) -> PolicyDecision:
    kind = request.action_type
    if kind not in _allowed_types(config):
        return PolicyDecision(Outcome.DENIED, None, (f"{kind!r} is not on the allowlist",))

    outbound = kind in OUTBOUND_TYPES
    blocks: list[str] = []
    if outbound:
        if request.contact_suppressed:
            blocks.append("suppressed")
        if request.injection_flagged:
            blocks.append("injection_flagged")
        if kind in EMAIL_BODY_TYPES and request.claim_check_passed is not True:
            blocks.append("claim_check_failed")
        if request.discount_pct is not None and (
            config.max_discount_pct is None or request.discount_pct > config.max_discount_pct
        ):
            blocks.append("discount_over_maximum")
        if kind == "send_io" and request.io_validated is not True:
            blocks.append("io_validation_failed")
    if blocks:
        return PolicyDecision(Outcome.BLOCKED, None, tuple(blocks), held=kill_switch and outbound)

    if kind == "attach_case_study" and not request.from_library:
        reasons = ("case study is not from the approved library",)
        return PolicyDecision(Outcome.DENIED, None, reasons)

    key = f"{kind}.{request.tier.value}" if kind == "first_touch" and request.tier else kind
    if key not in config.levels:
        return PolicyDecision(Outcome.DENIED, None, (f"no autonomy level configured for {key!r}",))

    level = config.levels[key]
    reasons = [f"level {level.value} for {key}"]

    if kind in EMAIL_BODY_TYPES and mentions_price(request.body):
        if request.rate_card_ok is True:
            reasons.append("mentions price, inside the rate card")
            if _RANK[config.levels[PRICE_RULE_KEY]] > _RANK[level]:
                level = config.levels[PRICE_RULE_KEY]
        else:
            reasons.append("mentions price, outside or unknown to the rate card")
            level = AutonomyLevel.NEEDS_APPROVAL

    if mode is AutonomyMode.APPROVAL and level is AutonomyLevel.AUTO_WITH_VETO:
        level = AutonomyLevel.NEEDS_APPROVAL
        reasons.append("approval mode: no veto windows")

    held = kill_switch and outbound
    if held:
        reasons.append("kill switch on: waiting")

    if kind in DRAFT_ONLY_TYPES:
        return PolicyDecision(Outcome.DRAFT_ONLY, level, (*reasons, "draft only"), held)
    return PolicyDecision(_OUTCOME[level], level, tuple(reasons), held)
