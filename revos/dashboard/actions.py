"""Everything the dashboard can DO. Each action calls the function the rest of the system already uses
(veto, human decision, dead-letter retry, the settings setters), so the same locks, audit rows and
re-reads apply. Nothing here touches a deal's stage or sends anything itself."""

from dataclasses import dataclass
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from revos.domain.enums import AutonomyMode
from revos.policy.audit import write_audit
from revos.policy.service import decide_by_human, veto_action
from revos.policy.settings import (
    resume_after_kill_switch,
    set_autonomy_mode,
    set_kill_switch,
    set_veto_window,
)
from revos.ports.clock import Clock
from revos.runtime.events import record_event
from revos.runtime.queue import retry_dead_letter

ACTOR = "dashboard"


@dataclass(frozen=True)
class Result:
    ok: bool
    message: str


def _deal_of(conn: psycopg.Connection, action_id: UUID):
    row = conn.execute("SELECT deal_id FROM actions WHERE id = %s", (action_id,)).fetchone()
    return row[0] if row else None


def cancel_send(conn: psycopg.Connection, clock: Clock, action_id: UUID) -> Result:
    """Cancel a send inside its veto window (the same compare-and-set the Slack button uses)."""
    try:
        veto_action(conn, clock, action_id, ACTOR)
    except LookupError:
        return Result(False, "That action no longer exists.")
    except ValueError:
        return Result(False, "The window has closed: it is already being sent.")
    write_audit(conn, ACTOR, "dashboard.cancelled", _deal_of(conn, action_id), {"action_id": str(action_id)})
    record_event(conn, f"slack:{action_id}:cancel", "slack.sync", {"action_id": str(action_id)}, clock)
    return Result(True, "Cancelled. It will not be sent.")


def decide(conn: psycopg.Connection, clock: Clock, action_id: UUID, approve: bool) -> Result:
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT status, decision_by FROM actions WHERE id = %s", (action_id,)).fetchone()
    if row is None:
        return Result(False, "That action no longer exists.")
    if row["status"] == "cancelled" and row["decision_by"] == "policy":
        return Result(False, "Blocked by policy: it cannot be approved or rejected.")
    if row["status"] != "awaiting_approval":
        return Result(False, f"Nothing to decide: the action is {row['status']}.")
    try:
        decide_by_human(conn, clock, action_id, ACTOR, approve, "approved in the dashboard" if approve
                        else "rejected in the dashboard")
    except ValueError:
        return Result(False, "Someone else already decided this.")
    if approve:                       # the send step re-reads the approval from the database before it sends
        record_event(conn, f"approved-send:{action_id}", "action.send", {"action_id": str(action_id)}, clock)
    record_event(conn, f"slack:{action_id}:{'approve' if approve else 'reject'}", "slack.sync",
                 {"action_id": str(action_id)}, clock)
    return Result(True, "Approved. It is queued to send." if approve else "Rejected. It will not be sent.")


def retry_job(conn: psycopg.Connection, clock: Clock, dead_letter_id: int) -> Result:
    try:
        job = retry_dead_letter(conn, dead_letter_id, clock, actor=ACTOR)
    except (LookupError, ValueError) as exc:
        return Result(False, str(exc))
    return Result(True, f"Job {job} is back on the queue.")


def set_kill(conn: psycopg.Connection, clock: Clock, on: bool) -> Result:
    if on:
        set_kill_switch(conn, True, ACTOR)
        return Result(True, "Kill switch ON: no email or message will be sent.")
    released = resume_after_kill_switch(conn, clock, ACTOR)
    return Result(True, f"Kill switch off. {len(released)} held send(s) went back on the queue.")


def set_mode(conn: psycopg.Connection, mode: str) -> Result:
    try:
        set_autonomy_mode(conn, AutonomyMode(mode), ACTOR)
    except ValueError:
        return Result(False, "Autonomy mode must be 'autonomous' or 'approval'.")
    return Result(True, f"Autonomy mode is now {mode}.")


def set_veto(conn: psycopg.Connection, raw: str) -> Result:
    raw = raw.strip()
    try:
        set_veto_window(conn, None if raw == "" else int(raw), ACTOR)
    except ValueError as exc:
        return Result(False, str(exc) if "veto window" in str(exc) else "Enter a whole number of seconds.")
    return Result(True, "Veto window override cleared: the environment decides again." if raw == ""
                  else f"Veto window is now {int(raw)} seconds for new actions.")
