"""What the CRM should hold about a deal, worked out from our own records."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from revos.domain.stages import DealStage

_STATUS = {
    DealStage.NEW: "queued", DealStage.ENRICHED: "queued", DealStage.SCORED: "queued",
    DealStage.OUTREACH: "first_touch_sent", DealStage.REPLIED: "replied",
    DealStage.MEETING: "meeting_booked", DealStage.QUALIFIED: "in_conversation",
    DealStage.PROPOSAL: "in_conversation", DealStage.IO_APPROVAL: "in_conversation",
    DealStage.OUT_FOR_SIGNATURE: "out_for_signature", DealStage.CLOSED_WON: "won",
    DealStage.HANDOFF: "won", DealStage.CLOSED_LOST: "lost", DealStage.DO_NOT_CONTACT: "opted_out",
}


def outreach_status(stage: DealStage | str) -> str:
    return _STATUS[DealStage(stage)]


def sequence_day(first_sent: datetime | None, now: datetime) -> int | None:
    """Day 1 is the day the first touch went out."""
    return None if first_sent is None else (now.date() - first_sent.date()).days + 1


def call_summary(title: str, fields: Mapping[str, Any], lesson_ids: list[int] | None = None) -> str:
    """A CRM note for a finished call: what was said, each with the words it rests on, and the
    lessons it taught."""
    lines = [f"Call summary: {title}"]
    for name in ("budget", "timeline", "decision_maker", "signer", "needs", "objections", "competitors",
                 "next_steps"):
        if fields.get(name):
            quote = fields.get(f"quote:{name}")
            lines.append(f"- {name.replace('_', ' ')}: {fields[name]}"
                         + (f'  [quote: "{quote}"]' if quote and quote != fields[name] else ""))
    for role in ("decision_maker", "signer"):
        if fields.get(f"{role}_name"):
            lines.append(f"- {role.replace('_', ' ')} (name, role): {fields[f'{role}_name']}, "
                         f"{fields.get(f'{role}_role', 'role not stated')}")
    if lesson_ids:
        lines.append("- lessons written: " + ", ".join(f"L{i}" for i in lesson_ids))
    return "\n".join(lines)
