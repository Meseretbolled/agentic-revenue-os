"""The lessons loop: record when a lesson was used, learn from what happened next, retire the ones
that fail.

Confidence moves by fixed steps (no model decides it). A use starts 'pending' and moves forward at
most once per kind of outcome, so replaying an event cannot change a lesson twice.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from revos.policy.audit import write_audit
from revos.ports.clock import Clock

RETIRE_BELOW = 0.25                  # confidence under this retires a lesson
MAX_LOSSES = 3                       # this many failures, with more failures than wins, retires it
NO_REPLY_AFTER = timedelta(days=7)   # silence this long after a use counts as a failure

# outcome -> (confidence change, counts as a win, counts as a loss)
OUTCOMES: dict[str, tuple[float, bool, bool]] = {
    "reply_positive": (+0.10, True, False),
    "meeting": (+0.15, True, False),
    "reply_negative": (-0.15, False, True),
    "no_reply": (-0.05, False, True),
}
# which pending states each outcome may move a use out of
_FROM = {"reply_positive": ("pending",), "meeting": ("pending", "reply_positive"),
         "reply_negative": ("pending",), "no_reply": ("pending",)}


@dataclass(frozen=True)
class LessonChange:
    lesson_id: int
    outcome: str
    confidence: float
    retired: str | None = None


def record_use(conn: psycopg.Connection, lesson_ids: list[int], deal_id: UUID, action_id: UUID,
               clock: Clock) -> list[int]:
    """A lesson shaped an action that went out. Counts once per (lesson, action)."""
    used = []
    with conn.transaction():
        for lesson_id in lesson_ids:
            row = conn.execute(
                "INSERT INTO lesson_uses (lesson_id, deal_id, action_id, used_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (lesson_id, action_id) WHERE action_id IS NOT "
                "NULL DO NOTHING RETURNING id", (lesson_id, deal_id, action_id, clock.now())
            ).fetchone()
            if row:
                conn.execute("UPDATE lessons SET uses = uses + 1 WHERE id = %s", (lesson_id,))
                used.append(lesson_id)
    return used


def _apply(cur: psycopg.Cursor, lesson_id: int, outcome: str, first_win: bool, clock: Clock,
           deal_id: UUID | None) -> LessonChange:
    delta, win, loss = OUTCOMES[outcome]
    lesson = cur.execute("SELECT confidence, wins, losses, retired_at FROM lessons WHERE id = %s "
                         "FOR UPDATE", (lesson_id,)).fetchone()
    confidence = round(min(1.0, max(0.0, float(lesson["confidence"]) + delta)), 2)
    wins = lesson["wins"] + (1 if win and first_win else 0)
    losses = lesson["losses"] + (1 if loss else 0)
    reason = None
    if lesson["retired_at"] is None:
        if confidence < RETIRE_BELOW:
            reason = f"confidence {confidence:.2f} fell below {RETIRE_BELOW:.2f}"
        elif losses >= MAX_LOSSES and losses > wins:
            reason = f"{losses} failures against {wins} successes"
    cur.execute(
        "UPDATE lessons SET confidence = %s, wins = %s, losses = %s, "
        "retired_at = coalesce(retired_at, CASE WHEN %s::text IS NULL THEN NULL ELSE %s END), "
        "retired_reason = coalesce(retired_reason, %s) WHERE id = %s",
        (confidence, wins, losses, reason, clock.now(), reason, lesson_id))
    if reason:
        write_audit(cur.connection, "lessons", "lesson.retired", deal_id,
                    {"lesson_id": lesson_id, "reason": reason, "confidence": confidence})
    return LessonChange(lesson_id, outcome, confidence, reason)


def record_outcome(conn: psycopg.Connection, deal_id: UUID, outcome: str,
                   clock: Clock) -> list[LessonChange]:
    """Something happened on this deal after lessons were used. Learn from it, once."""
    changes: list[LessonChange] = []
    with conn.transaction():
        cur = conn.cursor(row_factory=dict_row)
        uses = cur.execute(
            "SELECT id, lesson_id, outcome FROM lesson_uses WHERE deal_id = %s AND outcome = ANY(%s) "
            "ORDER BY id FOR UPDATE", (deal_id, list(_FROM[outcome]))).fetchall()
        for use in uses:
            cur.execute("UPDATE lesson_uses SET outcome = %s, outcome_at = %s WHERE id = %s",
                        (outcome, clock.now(), use["id"]))
            changes.append(_apply(cur, use["lesson_id"], outcome, use["outcome"] == "pending",
                                  clock, deal_id))
        if changes:
            write_audit(conn, "lessons", "lesson.outcome", deal_id, {
                "outcome": outcome, "lessons": [
                    {"id": c.lesson_id, "confidence": c.confidence, "retired": c.retired}
                    for c in changes]})
    return changes


def sweep_no_replies(conn: psycopg.Connection, clock: Clock) -> list[LessonChange]:
    """Lessons used a week ago with no reaction since count as a failure."""
    stale = conn.execute("SELECT DISTINCT deal_id FROM lesson_uses WHERE outcome = 'pending' AND "
                         "used_at <= %s", (clock.now() - NO_REPLY_AFTER,)).fetchall()
    changes: list[LessonChange] = []
    for (deal_id,) in stale:
        with conn.transaction():
            cur = conn.cursor(row_factory=dict_row)
            uses = cur.execute(
                "SELECT id, lesson_id FROM lesson_uses WHERE deal_id = %s AND outcome = 'pending' "
                "AND used_at <= %s ORDER BY id FOR UPDATE", (deal_id, clock.now() - NO_REPLY_AFTER)
            ).fetchall()
            for use in uses:
                cur.execute("UPDATE lesson_uses SET outcome = 'no_reply', outcome_at = %s "
                            "WHERE id = %s", (clock.now(), use["id"]))
                changes.append(_apply(cur, use["lesson_id"], "no_reply", False, clock, deal_id))
    return changes


def promote_to_global(conn: psycopg.Connection, lesson_id: int, actor: str, reason: str) -> None:
    """A person decides a lesson holds in every segment. Agents cannot do this."""
    with conn.transaction():
        done = conn.execute("UPDATE lessons SET segment_id = NULL WHERE id = %s AND retired_at IS "
                            "NULL AND segment_id IS NOT NULL", (lesson_id,)).rowcount
        if done:
            write_audit(conn, actor, "lesson.promoted", None, {"lesson_id": lesson_id,
                                                                  "reason": reason})


def citations_for_action(conn: psycopg.Connection, action_id: UUID) -> dict[str, Any]:
    """What an action cited, as the Slack message (Step 10) and the audit log will show it."""
    action = conn.cursor(row_factory=dict_row).execute(
        "SELECT payload FROM actions WHERE id = %s", (action_id,)).fetchone()
    payload = action["payload"] if action else {}
    ids = [int(i) for i in payload.get("lesson_ids", [])]
    lessons = conn.cursor(row_factory=dict_row).execute(
        "SELECT id, text, confidence, uses, wins, losses, source, retired_at FROM lessons "
        "WHERE id = ANY(%s) ORDER BY id", (ids,)).fetchall() if ids else []
    return {"lessons": [{**row, "confidence": float(row["confidence"]),
                         "retired": row.pop("retired_at") is not None} for row in lessons],
            "case_study": payload.get("case_study_id"), "angle": payload.get("angle")}
