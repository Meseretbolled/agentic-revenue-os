"""Lead scoring: fit, intent and engagement, a tier, and a written reason for every point.

`score_lead` is a pure function of the lead's facts and the pack's ScoringConfig. The optional
LLM nudge is applied afterwards, bounded, and can never touch a disqualified lead.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

from revos.domain.enums import Tier
from revos.segments.schema import MAX_NUDGE, Condition, ScoringConfig, ScoringRule

Facts = Mapping[str, Any]
_FALSE_WORDS = {"", "false", "no", "0", "n"}


@dataclass(frozen=True)
class ScoreReason:
    category: str          # fit | intent | engagement | disqualifier | tier | nudge
    rule_id: str
    points: int
    text: str


@dataclass(frozen=True)
class ScoreResult:
    fit: int
    intent: int
    engagement: int
    total: int
    tier: Tier
    reasons: tuple[ScoreReason, ...]
    disqualified_by: tuple[str, ...] = ()

    def reasons_for(self, category: str) -> list[ScoreReason]:
        return [r for r in self.reasons if r.category == category]

    @property
    def summary(self) -> str:
        """The written reason, one line per point awarded."""
        lines = [f"Tier {self.tier.value}: total {self.total}/100 "
                 f"(fit {self.fit}, intent {self.intent}, engagement {self.engagement})"]
        for r in self.reasons:
            sign = f"{r.points:+d}" if r.points else " 0"
            lines.append(f"  [{r.category}] {sign} {r.text}")
        return "\n".join(lines)


@dataclass(frozen=True)
class Nudge:
    """A judgment call from an LLM. It must say why and cite the evidence it used."""

    delta: int
    reason: str
    evidence: tuple[str, ...] = ()


class NudgeProvider(Protocol):
    def propose(self, facts: Facts, result: ScoreResult) -> Nudge | None: ...


def _half_up(x: float) -> int:
    return math.floor(x + 0.5)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_WORDS
    return bool(value)


def matches(cond: Condition, facts: Facts) -> bool:
    """Missing or wrongly typed facts never match: no guessing."""
    value = facts.get(cond.field)
    if value is None:
        return False
    op, target = cond.op, cond.value
    if op == "truthy":
        return _truthy(value)
    if op == "eq":
        return bool(value == target)
    if op == "ne":
        return bool(value != target)
    if op in {"in", "not_in"}:
        try:
            found = value in target
        except TypeError:
            return False
        return found if op == "in" else not found
    if op == "contains_any":
        return isinstance(value, list | tuple | set) and any(item in target for item in value)
    if not _is_number(value):
        return False
    if op == "between":
        return bool(target[0] <= value <= target[1])
    return {"gt": value > target, "gte": value >= target,
            "lt": value < target, "lte": value <= target}[op] if _is_number(target) else False


def _score_category(
    name: str, rules: list[ScoringRule], facts: Facts
) -> tuple[int, list[ScoreReason]]:
    reasons = [
        ScoreReason(name, rule.id, rule.points, rule.reason.format(value=facts.get(rule.field)))
        for rule in rules if matches(rule, facts)
    ]
    raw = sum(r.points for r in reasons)
    if raw > 100:
        reasons.append(ScoreReason(name, "cap", 100 - raw, f"{name} is capped at 100"))
    return min(raw, 100), reasons


def _tier_for(total: int, config: ScoringConfig) -> Tier:
    if total >= config.tiers.A:
        return Tier.A
    if total >= config.tiers.B:
        return Tier.B
    if total >= config.tiers.C:
        return Tier.C
    return Tier.DISQUALIFIED


def score_lead(facts: Facts, config: ScoringConfig) -> ScoreResult:
    fit, fit_reasons = _score_category("fit", config.fit_rules, facts)
    intent, intent_reasons = _score_category("intent", config.intent_rules, facts)
    engagement, eng_reasons = _score_category("engagement", config.engagement_rules, facts)
    w = config.weights
    total = _half_up(w.fit * fit + w.intent * intent + w.engagement * engagement)

    blocks = [d for d in config.disqualifiers if matches(d, facts)]
    reasons = [*fit_reasons, *intent_reasons, *eng_reasons]
    reasons += [ScoreReason("disqualifier", d.id, 0, d.reason.format(value=facts.get(d.field)))
                for d in blocks]

    if blocks:
        tier = Tier.DISQUALIFIED
        verdict = "disqualified by rule: " + ", ".join(d.id for d in blocks)
    else:
        tier = _tier_for(total, config)
        verdict = (f"total {total} (fit {fit} x {w.fit:g} + intent {intent} x {w.intent:g} + "
                   f"engagement {engagement} x {w.engagement:g}) gives tier {tier.value}")
        if tier is Tier.DISQUALIFIED:
            verdict += f": below the minimum of {config.tiers.C}"
    reasons.append(ScoreReason("tier", "tier", 0, verdict))
    return ScoreResult(fit, intent, engagement, total, tier, tuple(reasons),
                       tuple(d.id for d in blocks))


def apply_nudge(result: ScoreResult, nudge: Nudge | None, config: ScoringConfig) -> ScoreResult:
    """Add a bounded LLM judgment. Disqualified leads are returned unchanged."""
    if nudge is None:
        return result

    def ignored(why: str) -> ScoreResult:
        note = ScoreReason("nudge", "nudge_ignored", 0, f"LLM nudge ignored: {why}")
        return replace(result, reasons=(*result.reasons, note))

    if result.tier is Tier.DISQUALIFIED:
        return ignored("the lead is disqualified and a nudge can never override that")
    if not nudge.reason.strip() or not nudge.evidence:
        return ignored("it gave no reason or no cited evidence")

    limit = min(config.nudge_limit, MAX_NUDGE)
    delta = max(-limit, min(limit, nudge.delta))
    total = max(0, min(100, result.total + delta))
    delta = total - result.total
    clamped = "" if delta == nudge.delta else f" (asked for {nudge.delta:+d}, limit {limit})"
    note = ScoreReason("nudge", "nudge", delta,
                       f"LLM judgment {delta:+d}{clamped}: {nudge.reason} "
                       f"[evidence: {', '.join(nudge.evidence)}]")
    return replace(result, total=total, tier=_tier_for(total, config),
                   reasons=(*result.reasons, note))


def score_with_nudge(
    facts: Facts, config: ScoringConfig, provider: NudgeProvider | None = None
) -> ScoreResult:
    """Rules first; ask the provider only about leads the rules did not disqualify."""
    result = score_lead(facts, config)
    if provider is None or result.tier is Tier.DISQUALIFIED:
        return result
    try:
        nudge = provider.propose(facts, result)
    except Exception as exc:  # noqa: BLE001 - an unavailable LLM must never block scoring
        note = ScoreReason("nudge", "nudge_unavailable", 0,
                           f"LLM nudge unavailable ({type(exc).__name__}); rules-only score kept")
        return replace(result, reasons=(*result.reasons, note))
    return apply_nudge(result, nudge, config)
