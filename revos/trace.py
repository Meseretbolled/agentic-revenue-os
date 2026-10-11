"""`revos trace`: one deal's story, newest last, merged from every place the system records things.

Sources: queue events, agent runs and their proposals, policy decisions, outbox actions (and the CRM
writes among them), emails sent and received, replies held as ambiguous, lessons used, and the
audit log. Each entry says when, what happened and why. Message bodies are never included."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

ORDER = {"event": 0, "agent": 1, "proposal": 2, "policy": 3, "outbox": 4, "crm": 4, "email-out": 5,
         "email-in": 5, "held": 5, "lesson": 6, "audit": 7}


@dataclass
class Entry:
    at: datetime
    kind: str
    what: str
    why: str = ""
    correlation_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"at": self.at.isoformat(), "kind": self.kind, "what": self.what, "why": self.why,
                "correlation_id": self.correlation_id, "data": self.data}

    def line(self) -> str:
        tail = f"  [{self.correlation_id}]" if self.correlation_id else ""
        return (f"{self.at:%Y-%m-%d %H:%M:%S.%f}"[:-3] + f"  {self.kind:<9} {self.what}"
                + (f"  -- {self.why}" if self.why else "") + tail)


def find_deals(conn: psycopg.Connection, email: str) -> list[dict[str, Any]]:
    """Deals whose contact has this email, newest first."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT d.id, d.stage, d.created_at, co.name AS company FROM deals d "
        "JOIN contacts c ON c.id = d.contact_id LEFT JOIN companies co ON co.id = d.company_id "
        "WHERE lower(c.email) = %s ORDER BY d.created_at DESC", (email.strip().lower(),)).fetchall()


def timeline(conn: psycopg.Connection, deal_id: str) -> list[Entry]:
    cur = conn.cursor(row_factory=dict_row)
    entries: list[Entry] = []

    for r in cur.execute(
            "SELECT type, received_at, processed_at, idempotency_key, correlation_id FROM events "
            "WHERE payload->>'deal_id' = %(d)s OR payload->>'action_id' IN "
            "(SELECT id::text FROM actions WHERE deal_id = %(d)s::uuid)", {"d": deal_id}):
        state = "processed" if r["processed_at"] else "not processed yet"
        entries.append(Entry(r["received_at"], "event", f"event {r['type']} ({state})",
                             f"key {r['idempotency_key']}", r["correlation_id"]))

    for r in cur.execute("SELECT id, agent, step, runner, status, human_reason, started_at, "
                         "correlation_id FROM agent_runs WHERE deal_id = %s", (deal_id,)).fetchall():
        entries.append(Entry(r["started_at"], "agent", f"agent {r['agent']} ran ({r['step']}, "
                             f"runner {r['runner']}) -> {r['status']}", r["human_reason"] or "",
                             r["correlation_id"]))
        for p in cur.execute("SELECT idx, action_type, reason, status, outcome FROM agent_proposals "
                             "WHERE run_id = %s ORDER BY idx", (r["id"],)).fetchall():
            entries.append(Entry(r["started_at"], "proposal",
                                 f"{r['agent']} proposed {p['action_type']} -> {p['status']}"
                                 + (f" ({p['outcome']})" if p["outcome"] else ""), p["reason"],
                                 r["correlation_id"]))

    levels = {str(r["id"]): r["autonomy_level"] for r in cur.execute(
        "SELECT id, autonomy_level FROM actions WHERE deal_id = %s", (deal_id,)).fetchall()}
    for r in cur.execute("SELECT at, event, actor, detail, correlation_id FROM audit_log "
                         "WHERE deal_id = %s ORDER BY id", (deal_id,)).fetchall():
        d = r["detail"] or {}
        if r["event"] == "policy.decided":
            level = levels.get(d.get("action_id"), "?")
            reasons = "; ".join(d.get("reasons", [])) or "no reason recorded"
            held = " [held by kill switch]" if d.get("held") else ""
            entries.append(Entry(r["at"], "policy", f"policy decided {d.get('action_type')} -> "
                                 f"{d.get('outcome')} (level {level})",
                                 f"rule/reason: {reasons}{held}",
                                 r["correlation_id"], d))
        elif r["event"] == "deal.stage_changed":
            entries.append(Entry(r["at"], "audit", f"stage {d.get('from')} -> {d.get('to')}",
                                 f"{d.get('reason')} (by {r['actor']})", r["correlation_id"], d))
        else:
            why = "; ".join(f"{k}={d[k]}" for k in ("reason", "by", "outcome", "provider_id") if d.get(k))
            entries.append(Entry(r["at"], "audit", f"{r['event']} (by {r['actor']})", why,
                                 r["correlation_id"], d))

    for r in cur.execute("SELECT type, status, provider_id, decision_by, hold_reason, created_at, payload, "
                         "correlation_id FROM actions WHERE deal_id = %s", (deal_id,)).fetchall():
        crm = r["type"] in {"crm_update", "crm_note"}
        why = f"decided by {r['decision_by'] or 'nobody yet'}"
        if r["hold_reason"]:
            why += f"; {r['hold_reason']}"
        what = f"{'CRM write' if crm else 'outbox'} {r['type']}"
        if crm and r["payload"].get("kind"):
            what += f" ({r['payload']['kind']})"
        entries.append(Entry(r["created_at"], "crm" if crm else "outbox",
                             f"{what} [{r['status']}]" + (f" provider id {r['provider_id']}"
                                                          if r["provider_id"] else ""),
                             why, r["correlation_id"]))

    for r in cur.execute("SELECT direction, subject, message_id, in_reply_to, classification, quarantined, "
                         "created_at FROM messages WHERE deal_id = %s", (deal_id,)).fetchall():
        out = r["direction"] == "outbound"
        why = f"Message-ID {r['message_id']}"
        if r["in_reply_to"]:
            why += f", in reply to {r['in_reply_to']}"
        if r["classification"]:
            why += f", classified {r['classification']}"
        if r["quarantined"]:
            why += ", QUARANTINED"
        entries.append(Entry(r["created_at"], "email-out" if out else "email-in",
                             f"email {'sent' if out else 'received'}: {r['subject']!r}", why))

    for r in cur.execute("SELECT message_id, from_addr, subject, reason, created_at FROM held_replies "
                         "WHERE %s = ANY(candidate_deal_ids)", (deal_id,)).fetchall():
        entries.append(Entry(r["created_at"], "held", f"reply held, not attached to any deal: "
                             f"{(r['subject'] or '')[:60]!r} from {r['from_addr']}",
                             f"{r['reason']}; Message-ID {r['message_id']}"))

    for r in cur.execute("SELECT lu.lesson_id, lu.used_at, a.type FROM lesson_uses lu LEFT JOIN actions a "
                         "ON a.id = lu.action_id WHERE lu.deal_id = %s", (deal_id,)).fetchall():
        entries.append(Entry(r["used_at"], "lesson", f"lesson L{r['lesson_id']} used",
                             f"shaped the angle of {r['type'] or 'an action'}"))

    return sorted(entries, key=lambda e: (e.at, ORDER.get(e.kind, 9)))


def audit_trail(conn: psycopg.Connection, deal_id: str | None = None,
                limit: int = 40) -> list[dict[str, Any]]:
    where, args = ("WHERE deal_id = %s", (deal_id, limit)) if deal_id else ("", (limit,))
    rows = conn.cursor(row_factory=dict_row).execute(
        f"SELECT id, at, actor, event, deal_id, detail FROM audit_log {where} ORDER BY id DESC LIMIT %s",
        args).fetchall()
    return list(reversed(rows))


def held_replies(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Replies that could belong to more than one deal. Never attached to any; a person decides."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT message_id, from_addr, subject, received_at, reason, candidate_deal_ids FROM held_replies "
        "WHERE resolved_at IS NULL ORDER BY id").fetchall()


def held_calls(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Finished calls that matched no deal, or more than one. Held for a person; never guessed."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT external_id, title, started_at, reason, candidate_deal_ids FROM held_calls "
        "WHERE resolved_at IS NULL ORDER BY id").fetchall()
