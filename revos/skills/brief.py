"""The call brief for the next touch: where the deal stands, what is still open, what was learned."""

from collections.abc import Mapping, Sequence
from typing import Any

from revos.domain.drafting import Evidence

QUALIFICATION = ("budget", "timeline", "decision_maker", "needs")


def open_objections(fields: Mapping[str, Any]) -> list[str]:
    found = []
    if fields.get("objections"):
        found.append(f"raised on the call: {fields['objections']}")
    found += [f"{label.replace('_', ' ')} in a reply" for label in fields.get("open_objections", [])]
    return found


def build_brief(*, company: str, contact: str | None, stage: str, tier: str | None,
                total: int | None, fields: Mapping[str, Any], evidence: Evidence,
                lessons: Sequence[Mapping[str, Any]]) -> str:
    """Plain text for a CRM note. Every lesson it lists is cited by id."""
    who = contact or "unknown contact"
    if fields.get("decision_maker_name"):
        who += f"; decision maker {fields['decision_maker_name']}" + (
            f" ({fields['decision_maker_role']})" if fields.get("decision_maker_role") else "")
    if fields.get("signer_name"):
        who += f"; signer {fields['signer_name']}" + (
            f" ({fields['signer_role']})" if fields.get("signer_role") else "")
    score = f"tier {tier} ({total}/100)" if tier else "not scored"
    known = [f"{name}: {fields[name]}" for name in QUALIFICATION if fields.get(name)]
    facts = "; ".join(f"[{i.id}] {i.text}" for i in evidence.items.values()) or "none recorded"
    objections = open_objections(fields)
    used = "; ".join(f"L{item['id']} (confidence {float(item['confidence']):.2f}): {item['text']}"
                     for item in lessons) or "none"
    return "\n".join([
        f"Call brief: {company}",
        f"Deal: stage {stage}, {score}. Contact: {who}.",
        f"Known facts: {facts}.",
        f"Qualification so far: {'; '.join(known) if known else 'nothing yet'}.",
        f"Open objections: {'; '.join(objections) if objections else 'none recorded'}.",
        f"Lessons used: {used}.",
    ])
