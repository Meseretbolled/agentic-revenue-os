"""Plain-English wording for what the dashboard shows: sentences for audit events, who did it, how long ago.
Pure functions of data already on the page; nothing here reads or writes the database."""

from datetime import datetime
from typing import Any

PEOPLE = {"dashboard", "operator", "deal-desk", "owner"}
POLICY = {"policy"}

_ACTION = {
    "first_touch": "the first touch",
    "follow_up": "a follow-up",
    "crm_update": "a HubSpot update",
    "crm_note": "a HubSpot note",
    "send_io": "the IO for signature",
    "generate_io": "the IO document",
    "book_meeting": "the meeting invite",
    "handoff": "the closed-won handoff",
    "classify_reply": "a reply label",
    "enrich": "enrichment",
    "score": "the score",
    "lesson_write": "a lesson",
    "attach_case_study": "a case study",
}


def actor_kind(actor: str | None) -> str:
    """`person`, `policy` or `system`: the chip beside every event."""
    name = (actor or "").lower()
    if name in PEOPLE or name.startswith("slack:"):
        return "person"
    if name == "policy" or name.startswith("policy:"):
        return "policy"
    return "system"


def pretty(text: str | None) -> str:
    return (text or "").replace("_", " ").replace(".", " ").strip()


def stage_name(stage: str | None) -> str:
    return {
        "closed_won": "Closed Won",
        "closed_lost": "Closed Lost",
        "io_approval": "IO approval",
        "out_for_signature": "Out for signature",
    }.get(stage or "", pretty(stage).capitalize())


def ago(then: datetime | None, now: datetime) -> str:
    """'just now', '5m ago', '3h ago', '2d ago'."""
    if then is None:
        return "-"
    seconds = int((now - then).total_seconds())
    if seconds < 0:
        return "soon"
    if seconds < 45:
        return "just now"
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def sentence(
    event: str,
    actor: str | None,
    detail: dict[str, Any] | None,
    company: str | None = None,
    contact: str | None = None,
) -> str:
    """One readable sentence for an audit event. Falls back to the event name, never to raw JSON."""
    d = detail or {}
    who = company or "the deal"
    kind = d.get("type") or d.get("action_type") or ""
    thing = _ACTION.get(kind, pretty(kind) or "an action")
    if event == "deal.stage_changed":
        return f"Moved {who} to {stage_name(d.get('to'))}"
    if event == "action.sent":
        if kind in ("first_touch", "follow_up"):
            return f"Sent {thing} to {contact or 'the contact'}" + (f" at {company}" if company else "")
        return f"Sent {thing}" + (f" for {company}" if company else "")
    if event == "policy.decided":
        outcome = {
            "auto": "allowed automatically",
            "auto_with_veto": "allowed after a veto window",
            "needs_approval": "held for approval",
            "blocked": "blocked",
            "denied": "denied",
            "draft_only": "kept as a draft",
        }.get(str(d.get("outcome")), str(d.get("outcome")))
        return f"Policy {outcome}: {thing}"
    if event == "agent.run_started":
        return f"The {pretty(d.get('agent'))} agent started"
    if event == "agent.run_completed":
        return f"The {pretty(d.get('agent'))} agent finished"
    if event == "agent.proposal_rejected":
        return f"Rejected an agent proposal: {d.get('why') or 'not allowed'}"
    if event == "action.approved":
        return "Approved a send" + (f" ({d['reason']})" if d.get("reason") else "")
    if event == "action.vetoed":
        return "Cancelled a send inside its veto window"
    if event == "action.rejected":
        return "Rejected a send that needed approval"
    if event == "reply.classified":
        return f"Classified the reply from {contact or who} as {pretty(d.get('label'))}"
    if event == "reply.reprocessed":
        return f"Handled a reply again (attempt {d.get('attempt')})"
    if event == "inbound.matched":
        return f"Matched an incoming email to {who} (by {d.get('by')})"
    if event == "inbound.ambiguous":
        return "Held an incoming email: it could belong to several deals"
    if event == "pipeline.needs_human":
        return f"Stopped and asked a person: {pretty(d.get('reason'))}"
    if event == "meeting.booked":
        return f"Booked the meeting with {who}"
    if event == "slots.offered":
        return "Offered meeting times"
    if event == "call.matched":
        return f"Matched a call to {who}"
    if event in ("call.assigned", "call.reassigned"):
        return f"A person assigned a call to {who}"
    if event == "call.unmatched":
        return "Held a call that matched no single deal"
    if event == "call.qualification":
        return f"Checked the qualification criteria for {who}: {d.get('result')}"
    if event == "call.io":
        return f"Prepared the IO after the call ({d.get('result')})"
    if event == "deal.io_terms_set":
        return f"Set the IO terms ({d.get('source', 'auto')})"
    if event == "deal.signed":
        return f"Signature complete for {who}"
    if event.startswith("signature."):
        return f"Signature update: {event.split('.', 1)[1]}"
    if event == "job.retried":
        return f"Retried dead-lettered job {d.get('job_id')}"
    if event == "job.acknowledged":
        return f"Acknowledged dead letter {d.get('dead_letter_id')}"
    if event == "job.acknowledged_many":
        count = len(d.get("dead_letter_ids", []))
        return f"Acknowledged {count} dead letters older than {d.get('older_than_hours')}h"
    if event == "crm.write_superseded":
        return "Dropped an older HubSpot write that a newer one replaced"
    if event == "send_window.bypassed":
        return "Sent outside the send window (demo bypass)"
    if event == "domain_cap.bypassed":
        return "Sent past the per-domain cap (demo bypass)"
    if event == "gate.held":
        return f"Held a send ({d.get('reason')})"
    if event == "system.kill_switch":
        return "Kill switch turned " + ("ON" if d.get("value") else "off")
    if event == "system.autonomy_mode":
        return f"Autonomy mode set to {d.get('value')}"
    if event == "system.veto_window_seconds":
        return (
            "Veto window override cleared"
            if d.get("cleared")
            else f"Veto window set to {d.get('value')} seconds"
        )
    if event == "dashboard.cancelled":
        return "A person cancelled a send from the dashboard"
    if event.startswith("slack."):
        return "Slack: " + pretty(event.split(".", 1)[1])
    if event == "tracking.digest":
        return "Posted the daily digest"
    if event == "demo.reset":
        return "Cancelled queued demo work"
    return pretty(event).capitalize()
