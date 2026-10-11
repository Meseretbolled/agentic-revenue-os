"""What the dashboard reads. Plain SELECTs (and the existing `trace.timeline`); nothing here writes.

Free text from the database (inbound emails, quotes, audit details) is treated as untrusted: templates
escape it, and email addresses in it are masked before it reaches a page."""

import re
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from revos.domain.stages import DealStage
from revos.trace import timeline

STAGES = [s.value for s in DealStage]
BOARD_STAGES = [s for s in STAGES if s != DealStage.DO_NOT_CONTACT.value]
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def mask_email(address: str) -> str:
    """`meseretbolled18+3c5940cf@gmail.com` -> `m***@gmail.com`: the domain stays, the person does not."""
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain}" if domain else address


def mask(text: Any) -> str:
    return EMAIL.sub(lambda m: mask_email(m.group(0)), "" if text is None else str(text))


def age(since: datetime | None, now: datetime) -> str:
    if since is None:
        return "-"
    seconds = max(0, int((now - since).total_seconds()))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {rest // 60}m" if hours else f"{rest // 60}m" if rest >= 60 else f"{rest}s"


_NEXT_BY_STAGE = {
    "new": "Enrich the lead", "enriched": "Score the lead", "scored": "Draft the first touch",
    "outreach": "Wait for a reply", "replied": "Offer meeting times", "meeting": "Wait for the call",
    "qualified": "Propose IO terms", "proposal": "Validate the IO",
    "io_approval": "Send the IO for signature",
    "out_for_signature": "Wait for the signature", "closed_won": "Hand off to customer success",
    "handoff": "Done", "closed_lost": "Closed", "do_not_contact": "Do not contact",
}


def _next_action(stage: str, open_action: dict[str, Any] | None) -> str:
    if open_action:
        what = open_action["type"].replace("_", " ")
        return {"pending": f"{what}: in the veto window", "awaiting_approval": f"{what}: needs approval",
                "approved": f"{what}: being sent"}.get(open_action["status"], what)
    return _NEXT_BY_STAGE.get(stage, "-")


def board(conn: psycopg.Connection, now: datetime) -> dict[str, list[dict[str, Any]]]:
    cur = conn.cursor(row_factory=dict_row)
    columns: dict[str, list[dict[str, Any]]] = {s: [] for s in BOARD_STAGES}
    open_actions = {r["deal_id"]: r for r in cur.execute(
        "SELECT DISTINCT ON (deal_id) deal_id, type, status FROM actions WHERE status IN "
        "('pending', 'awaiting_approval', 'approved') AND deal_id IS NOT NULL "
        "ORDER BY deal_id, created_at DESC").fetchall()}
    owners = {r["deal_id"]: r["agent"] for r in cur.execute(
        "SELECT DISTINCT ON (deal_id) deal_id, agent FROM agent_runs ORDER BY deal_id, started_at DESC"
    ).fetchall()}
    for d in cur.execute(
            "SELECT d.id, d.stage, d.tier, d.total_score, d.created_at, d.fields, co.name AS company "
            "FROM deals d LEFT JOIN companies co ON co.id = d.company_id "
            "ORDER BY d.updated_at DESC").fetchall():
        if d["stage"] not in columns:
            continue
        columns[d["stage"]].append({
            "id": d["id"], "company": d["company"] or "(no company)", "tier": d["tier"],
            "score": d["total_score"],
            "owner": owners.get(d["id"], "-"), "age": age(d["created_at"], now),
            "next": _next_action(d["stage"], open_actions.get(d["id"])),
            "demo": bool((d["fields"] or {}).get("demo"))})
    return columns


def _score_rows(reason: str | None) -> tuple[str, list[dict[str, str]]]:
    """The scorer's written reason: a headline, then one `[rule] +points text` line per rule."""
    head, rows = "", []
    for line in (reason or "").splitlines():
        m = re.match(r"\s*\[(\w+)\]\s+([+-]\d+)\s+(.*)", line)
        if m:
            rows.append({"group": m.group(1), "points": m.group(2), "reason": m.group(3)})
        elif line.strip() and not head:
            head = line.strip()
    return head, rows


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text.casefold()).strip()


def deal(conn: psycopg.Connection, deal_id: str, now: datetime) -> dict[str, Any] | None:
    cur = conn.cursor(row_factory=dict_row)
    d = cur.execute(
        "SELECT d.*, co.name AS company, c.full_name AS contact FROM deals d LEFT JOIN companies co ON "
        "co.id = d.company_id LEFT JOIN contacts c ON c.id = d.contact_id WHERE d.id = %s",
        (deal_id,)).fetchone()
    if d is None:
        return None
    fields = d["fields"] or {}
    headline, rules = _score_rows(d["score_reason"])
    transcripts = " ".join(_norm(r["body"] or "") for r in cur.execute(
        "SELECT body FROM transcripts WHERE deal_id = %s", (deal_id,)).fetchall())
    calls = []
    for name in ("budget", "timeline", "decision_maker", "signer", "needs", "objections", "competitors",
                 "next_steps"):
        if fields.get(name):
            quote = str(fields.get(f"quote:{name}", ""))
            calls.append({"name": name.replace("_", " "), "value": fields[name], "quote": quote,
                          "exact": bool(quote) and _norm(quote) in transcripts})
    document = cur.execute("SELECT version, validation, created_at FROM documents WHERE deal_id = %s AND "
                           "kind = 'io' ORDER BY version DESC LIMIT 1", (deal_id,)).fetchone()
    io = None
    if fields.get("io_items") or document:
        validation = (document or {}).get("validation") or {}
        io = {"source": fields.get("io_terms_source") or "-", "package": fields.get("io_package") or "-",
              "start": fields.get("io_start_date"), "end": fields.get("io_end_date"),
              "discount": fields.get("discount_pct", 0), "items": fields.get("io_items") or [],
              "lines": validation.get("line_items") or [], "ok": validation.get("ok"),
              "problems": validation.get("problems") or [], "monthly": validation.get("monthly_subtotal"),
              "months": validation.get("months"), "total": validation.get("total"),
              "version": (document or {}).get("version")}
    emails = cur.execute("SELECT direction, subject, body, classification, quarantined, created_at "
                         "FROM messages "
                         "WHERE deal_id = %s ORDER BY created_at, id", (deal_id,)).fetchall()
    lessons = cur.execute(
        "SELECT l.id, l.text, l.evidence, lu.outcome, a.type AS used_in, lu.used_at FROM lesson_uses lu "
        "JOIN lessons l ON l.id = lu.lesson_id LEFT JOIN actions a ON a.id = lu.action_id "
        "WHERE lu.deal_id = %s ORDER BY lu.used_at", (deal_id,)).fetchall()
    pending = cur.execute(
        "SELECT id, type, status, veto_until, created_at FROM actions WHERE deal_id = %s AND status IN "
        "('pending', 'awaiting_approval', 'approved') ORDER BY created_at", (deal_id,)).fetchall()
    owner = cur.execute("SELECT agent FROM agent_runs WHERE deal_id = %s ORDER BY started_at DESC LIMIT 1",
                        (deal_id,)).fetchone()
    return {"id": d["id"], "company": d["company"], "contact": d["contact"], "stage": d["stage"],
            "tier": d["tier"], "score": d["total_score"], "parts": {k: d[k] for k in (
                "fit_score", "intent_score", "engagement_score")},
            "headline": headline, "rules": rules, "calls": calls, "io": io, "emails": emails,
            "lessons": lessons, "pending": pending, "age": age(d["created_at"], now),
            "owner": owner["agent"] if owner else "-", "demo": bool(fields.get("demo")),
            "timeline": [e for e in timeline(conn, str(deal_id))]}


def activity(conn: psycopg.Connection, actor: str = "", event: str = "", limit: int = 100
             ) -> dict[str, Any]:
    cur = conn.cursor(row_factory=dict_row)
    where, args = ["TRUE"], []
    if actor:
        where.append("a.actor = %s")
        args.append(actor)
    if event:
        where.append("a.event = %s")
        args.append(event)
    rows = cur.execute(
        f"SELECT a.id, a.at, a.actor, a.event, a.deal_id, a.detail, co.name AS company FROM audit_log a "
        f"LEFT JOIN deals d ON d.id = a.deal_id LEFT JOIN companies co ON co.id = d.company_id "
        f"WHERE {' AND '.join(where)} ORDER BY a.id DESC LIMIT %s",
        (*args, min(max(limit, 1), 500))).fetchall()
    return {"rows": rows,
            "actors": [r["actor"] for r in cur.execute("SELECT DISTINCT actor FROM audit_log ORDER BY 1")],
            "events": [r["event"] for r in cur.execute("SELECT DISTINCT event FROM audit_log ORDER BY 1")]}


def pending(conn: psycopg.Connection, now: datetime) -> dict[str, list[dict[str, Any]]]:
    cur = conn.cursor(row_factory=dict_row)
    base = ("SELECT a.id, a.type, a.status, a.veto_until, a.created_at, a.payload, a.deal_id, a.decision_by, "
            "co.name AS company FROM actions a LEFT JOIN deals d ON d.id = a.deal_id "
            "LEFT JOIN companies co ON co.id = d.company_id ")
    veto = cur.execute(base + "WHERE a.status = 'pending' AND a.veto_until > %s ORDER BY a.veto_until",
                       (now,)).fetchall()
    approval = cur.execute(base + "WHERE a.status = 'awaiting_approval' ORDER BY a.created_at").fetchall()
    # hard blocks only: cancelled by policy AND recorded by the engine as "blocked" (a superseded CRM write
    # is also cancelled by policy, but nothing was blocked)
    blocked = cur.execute(
        base + "WHERE a.status = 'cancelled' AND a.decision_by = 'policy' "
        "AND EXISTS (SELECT 1 FROM audit_log l "
        "WHERE l.event = 'policy.decided' AND l.detail->>'outcome' = 'blocked' "
        "AND l.detail->>'action_id' = a.id::text) ORDER BY a.created_at DESC LIMIT 20").fetchall()
    reasons = {r["action_id"]: r["reasons"] for r in cur.execute(
        "SELECT detail->>'action_id' AS action_id, detail->'reasons' AS reasons FROM audit_log "
        "WHERE event = 'policy.decided' AND detail->>'outcome' = 'blocked' ORDER BY id DESC LIMIT 200")}
    for item in (*veto, *approval, *blocked):
        payload = item["payload"] or {}
        item["subject"] = mask(payload.get("subject") or payload.get("title") or "")
        item["why"] = "; ".join(reasons.get(str(item["id"]), []) or [])
    return {"veto": veto, "approval": approval, "blocked": blocked}


def queue(conn: psycopg.Connection, now: datetime) -> dict[str, Any]:
    cur = conn.cursor(row_factory=dict_row)
    counts = {r["status"]: r["n"] for r in cur.execute(
        "SELECT status, count(*) AS n FROM jobs WHERE status IN ('queued', 'running') GROUP BY status")}
    oldest = cur.execute("SELECT min(run_at) AS at FROM jobs WHERE status = 'queued' AND run_at <= %s",
                         (now,)).fetchone()["at"]
    dead = cur.execute("SELECT id, job_id, type, error, created_at FROM dead_letters "
                       "WHERE retried_at IS NULL "
                       "ORDER BY id DESC LIMIT 100").fetchall()
    by_type = cur.execute("SELECT type, count(*) AS n FROM jobs WHERE status = 'queued' GROUP BY type "
                          "ORDER BY n DESC LIMIT 8").fetchall()
    return {"queued": counts.get("queued", 0), "running": counts.get("running", 0),
            "oldest": age(oldest, now) if oldest else "none waiting", "dead": dead, "by_type": by_type}


def lessons(conn: psycopg.Connection) -> list[dict[str, Any]]:
    cur = conn.cursor(row_factory=dict_row)
    rows = cur.execute("SELECT * FROM lessons ORDER BY retired_at IS NOT NULL, id DESC").fetchall()
    uses = cur.execute(
        "SELECT lu.lesson_id, lu.deal_id, lu.outcome, lu.used_at, a.type, co.name AS company "
        "FROM lesson_uses lu "
        "LEFT JOIN actions a ON a.id = lu.action_id LEFT JOIN deals d ON d.id = lu.deal_id "
        "LEFT JOIN companies co ON co.id = d.company_id ORDER BY lu.used_at DESC").fetchall()
    for lesson in rows:
        lesson["cited_by"] = [u for u in uses if u["lesson_id"] == lesson["id"]]
    return rows
