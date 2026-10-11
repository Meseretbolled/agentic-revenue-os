"""Block Kit messages for actions. Pure functions: a view of one action's current state.

A button carries ONLY the action id. Nothing else in the message is ever read back as an instruction;
when a button is pressed the app re-reads the action from the database."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

ACTION_CANCEL, ACTION_APPROVE, ACTION_REJECT = "revos_cancel", "revos_approve", "revos_reject"
BODY_PREVIEW = 600


@dataclass(frozen=True)
class ActionView:
    action_id: str
    action_type: str
    status: str                    # pending | awaiting_approval | approved | cancelled | sent | failed
    decision: str | None
    decision_by: str | None
    hold_reason: str | None
    lead: str                      # "Maya Ortiz at Harborview Optical Collective" (never the raw address)
    tier: str | None
    total_score: int | None
    score_reason: str
    subject: str | None
    body: str | None
    case_study: str | None         # "Pearle Vision (public testimonial)"
    lesson_ids: list[int] = field(default_factory=list)
    claims: int = 0
    claim_check: str = ""          # a plain sentence about the claim check
    draft_source: str = "model"
    veto_until: datetime | None = None
    sent_at: datetime | None = None
    decided_at: datetime | None = None
    seconds_left: int | None = None


def _text(text: str) -> dict[str, Any]:
    return {"type": "mrkdwn", "text": text}


def _fields(*pairs: tuple[str, str]) -> dict[str, Any]:
    return {"type": "section", "fields": [_text(f"*{k}*\n{v}") for k, v in pairs if v]}


def _clock(moment: datetime | None) -> str:
    return f"{moment:%Y-%m-%d %H:%M:%S} UTC" if moment else "unknown time"


def _who(decision_by: str | None) -> str:
    if not decision_by:
        return "someone"
    return f"<@{decision_by.split(':', 1)[1]}>" if decision_by.startswith("slack:") else decision_by


def _reasons(view: ActionView) -> str:
    lines = [ln.strip() for ln in (view.score_reason or "").splitlines() if ln.strip()]
    return "\n".join(lines[:6]) or "no written reason recorded"


def _label(view: ActionView) -> str:
    return view.action_type.replace("_", " ")


def render(view: ActionView) -> tuple[str, list[dict[str, Any]]]:
    """(fallback text, blocks) for the action in its current state."""
    state = _state(view)
    head = {"pending": f":hourglass_flowing_sand: {_label(view)} to *{view.lead}*",
            "awaiting_approval": f":raised_hand: Approval needed: {_label(view)} to *{view.lead}*",
            "approved": f":outbox_tray: Sending: {_label(view)} to *{view.lead}*",
            "sent": f":white_check_mark: Sent: {_label(view)} to *{view.lead}*",
            "cancelled": f":no_entry_sign: Cancelled: {_label(view)} to *{view.lead}*",
            "rejected": f":no_entry_sign: Rejected: {_label(view)} to *{view.lead}*",
            "blocked": f":stop_sign: Blocked: {_label(view)} to *{view.lead}*",
            "failed": f":warning: Failed: {_label(view)} to *{view.lead}*"}[state]
    blocks: list[dict[str, Any]] = [{"type": "section", "text": _text(head)}]

    if state == "blocked":
        reason = view.hold_reason or "blocked by policy"
        blocks.append({"type": "section", "text": _text(
            f"*Blocked: {reason}*\nThis cannot be approved or overridden from Slack. "
            "A person has to fix the cause.")})
    score = f"{view.total_score}/100, tier {view.tier}" if view.tier else "not scored"
    blocks.append(_fields(("Score", score),
                          ("Case study", view.case_study or "none"),
                          ("Lessons cited", ", ".join(f"L{i}" for i in view.lesson_ids) or "none"),
                          ("Claim check", view.claim_check or "no claims made")))
    blocks.append({"type": "section", "text": _text(f"*Why this lead* (written reason)\n{_reasons(view)}")})
    if view.subject or view.body:
        preview = (view.body or "")[:BODY_PREVIEW] + ("…" if len(view.body or "") > BODY_PREVIEW else "")
        blocks.append({"type": "section", "text": _text(
            f"*Subject:* {view.subject or '(none)'}\n```{preview}```")})

    note = {"pending": f"Sends in *{view.seconds_left if view.seconds_left is not None else 60} seconds* "
                       "unless someone cancels it.",
            "awaiting_approval": "Nothing is sent until someone approves it.",
            "approved": "Approved. Sending now.",
            "sent": f"Sent {_clock(view.sent_at)}.",
            "cancelled": f"Cancelled by {_who(view.decision_by)} at {_clock(view.decided_at)}.",
            "rejected": f"Rejected by {_who(view.decision_by)} at {_clock(view.decided_at)}.",
            "blocked": "", "failed": "The send failed; see `revos trace`."}[state]
    if note:
        blocks.append({"type": "context", "elements": [_text(note)]})

    if state == "pending":
        blocks.append({"type": "actions", "elements": [_button("Cancel", ACTION_CANCEL, view.action_id,
                                                              style="danger")]})
    elif state == "awaiting_approval":
        blocks.append({"type": "actions", "elements": [
            _button("Approve", ACTION_APPROVE, view.action_id, style="primary"),
            _button("Reject", ACTION_REJECT, view.action_id, style="danger")]})
    return head.replace("*", "").replace(":", "")[:150], blocks


def _button(label: str, action_id: str, value: str, *, style: str) -> dict[str, Any]:
    return {"type": "button", "text": {"type": "plain_text", "text": label}, "action_id": action_id,
            "value": value, "style": style}


def _state(view: ActionView) -> str:
    """One word for what the message should say, derived from the database row (never from Slack)."""
    if view.status == "sent":
        return "sent"
    if view.status == "failed":
        return "failed"
    if view.status == "cancelled":
        if view.decision == "vetoed":
            return "cancelled"
        if view.decision_by == "policy" or view.decision == "approved":
            return "blocked"             # decided by policy: a hard block, nothing to approve
        return "rejected"
    if view.status == "awaiting_approval":
        return "awaiting_approval"
    if view.status == "approved":
        return "approved"
    return "pending"
