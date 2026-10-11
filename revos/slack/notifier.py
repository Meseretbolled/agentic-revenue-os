"""Keeps one Slack message per action in step with the database.

`sync(action_id)` reads the action's CURRENT state from Postgres and either posts its message (the
first time) or updates the same message in place (chat.update). It is safe to call any number of
times: the message id is stored under a unique key, and a post is serialised per action."""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from revos.obs import log
from revos.ports.clock import Clock
from revos.ports.slack import Slack
from revos.segments.loader import Pack
from revos.slack.blocks import ActionView, _state, render

# What gets a Slack message: things that go out to a person or a customer.
POSTED_TYPES = frozenset({"first_touch", "follow_up", "send_io", "book_meeting", "handoff"})


class SlackNotifier:
    def __init__(self, conn: psycopg.Connection, slack: Slack, channel: str, clock: Clock,
                 packs: Mapping[str, Pack] | None = None) -> None:
        self.conn, self.slack, self.channel, self.clock = conn, slack, channel, clock
        self.packs = packs or {}

    # --- reading ------------------------------------------------------------------------------------
    def view(self, action_id: UUID) -> ActionView | None:
        row = self.conn.cursor(row_factory=dict_row).execute(
            "SELECT a.id, a.type, a.status, a.decision, a.decision_by, a.hold_reason, a.autonomy_level, "
            "a.payload, a.veto_until, a.sent_at, a.decided_at, d.tier, d.total_score, d.score_reason, "
            "d.segment_id, c.full_name, co.name AS company FROM actions a "
            "LEFT JOIN deals d ON d.id = a.deal_id LEFT JOIN contacts c ON c.id = a.contact_id "
            "LEFT JOIN companies co ON co.id = d.company_id WHERE a.id = %s", (action_id,)).fetchone()
        if row is None:
            return None
        payload: dict[str, Any] = row["payload"] or {}
        lead = f"{row['full_name'] or 'the contact'} at {row['company'] or 'their company'}"
        pack = self.packs.get(row["segment_id"])
        study = None
        if payload.get("case_study_id") and pack is not None:
            found = next((s for s in pack.case_studies if s.id == payload["case_study_id"]), None)
            study = f"{found.customer} ({found.label})" if found else str(payload["case_study_id"])
        elif payload.get("case_study_id"):
            study = str(payload["case_study_id"])
        left = None
        if row["veto_until"] is not None:
            left = max(0, int((row["veto_until"] - self.clock.now()).total_seconds()))
        return ActionView(
            action_id=str(row["id"]), action_type=row["type"], status=row["status"], decision=row["decision"],
            decision_by=row["decision_by"], hold_reason=row["hold_reason"], lead=lead,
            tier=row["tier"], total_score=row["total_score"], score_reason=row["score_reason"] or "",
            subject=payload.get("subject"), body=payload.get("body"), case_study=study,
            lesson_ids=[int(i) for i in payload.get("lesson_ids", [])],
            claims=len(payload.get("claims", [])), claim_check=_claim_check(payload, row["status"]),
            draft_source=str(payload.get("draft_source", "model")), veto_until=row["veto_until"],
            sent_at=row["sent_at"], decided_at=row["decided_at"], seconds_left=left)

    def wants_message(self, action_id: UUID) -> bool:
        view = self.view(action_id)
        if view is None or view.action_type not in POSTED_TYPES:
            return False
        row = self.conn.execute("SELECT autonomy_level FROM actions WHERE id = %s", (action_id,)).fetchone()
        state = _state(view)
        return (row[0] == "auto_with_veto" or state in {"awaiting_approval", "blocked", "rejected"}
                or self._has_message(action_id))

    def _has_message(self, action_id: UUID) -> bool:
        return self.conn.execute("SELECT 1 FROM slack_messages WHERE kind = 'action' AND ref = %s",
                                 (str(action_id),)).fetchone() is not None

    # --- writing ------------------------------------------------------------------------------------
    def sync(self, action_id: UUID) -> str:
        """Post the message, or update it in place. Returns 'posted', 'updated', 'unchanged' or 'skipped'."""
        if not self.wants_message(action_id):
            return "skipped"
        view = self.view(action_id)
        assert view is not None
        state = _state(view)
        text, blocks = render(view)
        with self.conn.transaction():
            self.conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"slack:{action_id}",))
            existing = self.conn.execute(
                "SELECT channel, ts, state FROM slack_messages WHERE kind = 'action' AND ref = %s",
                (str(action_id),)).fetchone()
            if existing is None:
                posted = self.slack.post(self.channel, text, blocks)
                self.conn.execute("INSERT INTO slack_messages (kind, ref, channel, ts, state) "
                                  "VALUES ('action', %s, %s, %s, %s)",
                                  (str(action_id), posted.channel, posted.ts, state))
                log("slack", "posted", deal_id=None, action_id=str(action_id), state=state)
                return "posted"
            channel, ts, last = existing
            if last == state:
                return "unchanged"
            self.slack.update(channel, ts, text, blocks)
            self.conn.execute("UPDATE slack_messages SET state = %s WHERE kind = 'action' AND ref = %s",
                              (state, str(action_id)))
            log("slack", "updated", action_id=str(action_id), state=state)
            return "updated"

    def message_for(self, action_id: UUID) -> tuple[str, str] | None:
        row = self.conn.execute("SELECT channel, ts FROM slack_messages WHERE kind = 'action' AND ref = %s",
                                (str(action_id),)).fetchone()
        return (row[0], row[1]) if row else None


def _claim_check(payload: Mapping[str, Any], status: str) -> str:
    claims = payload.get("claims") or []
    if payload.get("fallback"):
        failed = ", ".join(payload["fallback"].get("checks_failed", []))
        return f"model text failed ({failed}); the pack's own wording is used, no claims"
    if claims:
        cited = sorted({e for c in claims for e in c.get("evidence", [])})
        return f"{len(claims)} claim(s), each checked against its evidence ({', '.join(cited)}): passed"
    return "no claims about the prospect; code checks passed" if status != "cancelled" else ""
