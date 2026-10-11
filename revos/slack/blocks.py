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


def _first_name(lead: str) -> str:
    return lead.split(" at ", 1)[0].split()[0] if lead.strip() else "the contact"


def _company(lead: str) -> str:
    return lead.split(" at ", 1)[1] if " at " in lead else lead


def _chunks(text: str, size: int = 1800) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def render(view: ActionView) -> tuple[str, list[dict[str, Any]]]:
    """(fallback text, blocks) for the action in its current state.

    Layout, so the button is never lost: a one-line header, the buttons directly under it, a compact
    summary, the details in small print, and the same buttons again at the very bottom."""
    state = _state(view)
    who, company = _first_name(view.lead), _company(view.lead)
    what = _label(view)
    secs = view.seconds_left if view.seconds_left is not None else 60
    head = {"pending": f":hourglass_flowing_sand: *Sending to {who} at {company} in {secs} seconds*",
            "awaiting_approval": f":raised_hand: *Approval needed: {what} to {who} at {company}*",
            "approved": f":outbox_tray: *Sending {what} to {who} at {company} now*",
            "sent": f":white_check_mark: *Sent {_clock(view.sent_at)}: {what} to {who} at {company}*",
            "cancelled": f":no_entry_sign: *Cancelled by {_who(view.decision_by)} at "
                         f"{_clock(view.decided_at)}: {what} to {who} at {company}*",
            "rejected": f":no_entry_sign: *Rejected by {_who(view.decision_by)} at "
                        f"{_clock(view.decided_at)}: {what} to {who} at {company}*",
            "blocked": f":stop_sign: *Blocked: {view.hold_reason or 'blocked by policy'}* "
                       f"({what} to {who} at {company})",
            "failed": f":warning: *Failed: {what} to {who} at {company}*"}[state]
    buttons = {"pending": [("Cancel", ACTION_CANCEL, "danger")],
               "awaiting_approval": [("Approve", ACTION_APPROVE, "primary"),
                                     ("Reject", ACTION_REJECT, "danger")]}.get(state, [])

    def actions(block_id: str) -> dict[str, Any]:
        return {"type": "actions", "block_id": block_id, "elements": [
            _button(label, action, view.action_id, style=style) for label, action, style in buttons]}

    blocks: list[dict[str, Any]] = [{"type": "section", "block_id": "head", "text": _text(head)}]
    if buttons:
        blocks.append(actions("buttons_top"))

    top_reason = next((ln.strip() for ln in (view.score_reason or "").splitlines()[1:] if ln.strip()), "")
    score = f"*Tier {view.tier} · {view.total_score}/100*" if view.tier else "*Not scored*"
    summary = [score + (f" — {top_reason}" if top_reason else ""), f"*Subject:* {view.subject or '(none)'}"]
    if view.case_study:
        summary.append(f"*Case study:* {view.case_study}")
    if state == "blocked":
        summary.insert(0, "This cannot be approved or overridden from Slack; a person has to fix the cause.")
    blocks.append({"type": "section", "block_id": "summary", "text": _text("\n".join(summary))})

    small = [f"Lessons cited: {', '.join(f'L{i}' for i in view.lesson_ids) or 'none'}",
             f"Claim check: {view.claim_check or 'no claims made'}"]
    if state == "approved":
        small.append("Approved. Sending now.")
    elif state == "failed":
        small.append("The send failed; see `revos trace`.")
    blocks.append({"type": "context", "block_id": "facts", "elements": [_text(" · ".join(small))]})

    detail = f"*Why this lead (written reason)*\n{_reasons(view)}"
    if view.body:
        preview = view.body[:BODY_PREVIEW] + ("…" if len(view.body) > BODY_PREVIEW else "")
        detail += f"\n*Draft*\n{preview}"
    blocks.append({"type": "context", "block_id": "details",
                   "elements": [_text(chunk) for chunk in _chunks(detail)[:3]]})

    if buttons:
        blocks.append(actions("buttons_bottom"))        # the same buttons again, reachable without scrolling
    fallback = head.replace("*", "")
    for token in (":hourglass_flowing_sand: ", ":raised_hand: ", ":outbox_tray: ", ":white_check_mark: ",
                  ":no_entry_sign: ", ":stop_sign: ", ":warning: "):
        fallback = fallback.replace(token, "")
    return fallback[:150], blocks


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
