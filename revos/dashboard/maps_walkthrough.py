# ruff: noqa: E501  (the node and step descriptions are prose, kept on one line each)
"""The animated walkthrough: one deal's real journey, step by step, on a fixed diagram of the system.

The diagram, the sixteen steps and their wording are code (what the system does is the same for every deal). What
is *recorded* under each step is read from that deal's own audit log: the first matching rows, with their real
timestamps. A step with no matching row says so; nothing is filled in. Nothing here writes."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from revos.agents.registry import AGENTS
from revos.dashboard import data, maps_data
from revos.mcp.agents import REGISTRY
from revos.runtime.scheduler import Schedule

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

# id: (column x, y, width, height, title, what it does). Subtitles that are numbers come from the code below.
NODES: dict[str, tuple[int, int, int, int, str, str]] = {
    "cli": (10, 50, 150, 56, "CLI", "A command seeds a lead, runs the system or drafts a message. It only creates events."),
    "scheduler": (10, 120, 150, 56, "Scheduler", "Enqueues each recurring event once per time bucket, so two schedulers cannot duplicate a tick."),
    "webhooks": (10, 190, 150, 56, "Webhooks", "Slack button presses and Dropbox Sign callbacks. Both are signature-checked; nothing else is reachable from outside."),
    "pollers": (10, 260, 150, 56, "Pollers", "Read Gmail replies and finished Fireflies calls on a timer, once each."),
    "events": (190, 50, 170, 70, "Events + queue", "Every trigger becomes an event with a unique key (a duplicate is a no-op), then jobs claimed with SKIP LOCKED, retried, then dead-lettered."),
    "orchestrator": (190, 190, 170, 90, "Orchestrator", "Owns the deal. Runs the job handlers, asks agents for proposals, and moves the stage only through the state machine's one guarded function."),
    "packs": (190, 330, 170, 56, "Segment packs", "Each segment is YAML: scoring, wording, policy, rate card, case studies, stage criteria. No Python names a segment."),
    "postgres": (190, 520, 400, 56, "Postgres", "The only state: deals, queue, outbox, audit. The source of truth."),
    "agents": (395, 50, 190, 80, "Agents", "Agents only propose. Some are plain rules, some use a model for judgement; none can send or approve anything."),
    "skills": (395, 150, 190, 56, "Skill modules", "Pure functions: scoring, claim checks, slot picking, IO validation, drafting checks."),
    "tools": (395, 220, 190, 56, "Read-only tools", "The only things an agent can call, each scoped to that agent. An unresolvable scope blocks the call."),
    "llm": (395, 290, 190, 56, "Claude Agent SDK", "One call per model task, no built-in tools, spend-capped by a ledger that fails closed."),
    "research": (395, 360, 190, 56, "Web research", "Offline mock by default; real Tavily only when switched on. A result is kept only if domain, name and location match the lead."),
    "lessons": (395, 430, 190, 56, "Lessons store", "Quoted lessons found by tag and keyword. A lesson only picks the angle, never the facts."),
    "policy": (615, 50, 140, 436, "Policy engine", "An allowlist: unknown actions are denied. Hard blocks (failed claim check, injection flag, suppression, discount over max) never send."),
    "veto": (785, 50, 150, 56, "Veto window", "Top-tier first touches wait for a cancel window. Cancel is a compare-and-set, so cancel and send cannot both win."),
    "approval": (785, 130, 150, 56, "Approval queue", "Actions that need a person wait in Slack or the dashboard."),
    "draft": (785, 210, 150, 56, "Draft only", "LinkedIn drafts: there is no provider for them, so nothing can send them."),
    "gate": (965, 300, 130, 76, "Send gate", "Re-reads the approval from the database and checks the kill switch, the recipient allowlist, the send window and the caps."),
    "outbox": (965, 400, 130, 56, "Outbox", "Every side effect is a row with a unique key. Before any retry it asks the provider whether it already happened."),
    "audit": (965, 490, 130, 56, "Audit log", "Append-only, written in the same transaction as the change."),
    "gmail": (1125, 50, 125, 56, "Gmail", "Outbound email and inbound replies."),
    "hubspot": (1125, 120, 125, 56, "HubSpot", "The CRM, the system of record."),
    "slack": (1125, 190, 125, 56, "Slack", "Approvals, the veto window, the digest, the handoff message."),
    "dropbox": (1125, 260, 125, 56, "Dropbox Sign", "E-signature. Test mode only: nothing is legally binding."),
    "fireflies": (1125, 330, 125, 56, "Fireflies", "Call transcripts."),
    "calendar": (1125, 400, 125, 56, "Calendar", "A mock: no real calendar is connected."),
}

# (id, from, to, label, from-side, to-side)
EDGES: list[tuple[str, str, str, str, str, str]] = [
    ("cli-events", "cli", "events", "", "r", "l"),
    ("scheduler-events", "scheduler", "events", "", "r", "l"),
    ("webhooks-events", "webhooks", "events", "", "r", "l"),
    ("pollers-events", "pollers", "events", "", "r", "l"),
    ("events-orchestrator", "events", "orchestrator", "", "b", "t"),
    ("orchestrator-agents", "orchestrator", "agents", "asks", "r", "l"),
    ("agents-policy", "agents", "policy", "proposes", "r", "l"),
    ("policy-veto", "policy", "veto", "veto", "r", "l"),
    ("policy-approval", "policy", "approval", "ask", "r", "l"),
    ("policy-draft", "policy", "draft", "draft", "r", "l"),
    ("policy-gate", "policy", "gate", "auto", "r", "l"),
    ("veto-gate", "veto", "gate", "", "r", "t"),
    ("approval-gate", "approval", "gate", "", "r", "t"),
    ("gate-outbox", "gate", "outbox", "", "b", "t"),
    ("outbox-audit", "outbox", "audit", "", "b", "t"),
    ("outbox-gmail", "outbox", "gmail", "", "r", "l"),
    ("outbox-hubspot", "outbox", "hubspot", "", "r", "l"),
    ("outbox-slack", "outbox", "slack", "", "r", "l"),
    ("outbox-dropbox", "outbox", "dropbox", "", "r", "l"),
]

# Each step: title, text, nodes and edges lit, the stage chip lit, and what to look up in this deal's audit log.
# `record` = (event name, optional "to" stage for stage changes).
STEPS: list[dict[str, Any]] = [
    {"title": "A lead enters", "stage": "new", "nodes": ["cli", "events", "postgres"], "edges": ["cli-events"],
     "text": "A command seeds the lead. It becomes an event with a unique key, so repeating the command cannot create "
             "a second deal. The orchestrator creates the deal and Postgres holds its state.",
     "record": [("lead.created", None)]},
    {"title": "The queue hands it to the orchestrator", "stage": "new", "nodes": ["events", "orchestrator"],
     "edges": ["events-orchestrator"],
     "text": "A worker claims the job with a lease. If it crashes, another worker reclaims the job after the lease runs "
             "out. Nothing is lost and nothing runs twice.",
     "record": [("agent.run_started", None)]},
    {"title": "Enrichment fills in the lead", "stage": "enriched", "nodes": ["orchestrator", "agents", "research", "tools"],
     "edges": ["orchestrator-agents"],
     "text": "A rules-based agent reads only the company's own site, or runs web research when it is switched on. Every "
             "fact is stored with its source and the sentence it came from.",
     "record": [("enrichment.completed", None), ("deal.stage_changed", "enriched")]},
    {"title": "Scoring ranks the lead", "stage": "scored", "nodes": ["orchestrator", "agents", "skills", "packs"],
     "edges": ["orchestrator-agents"],
     "text": "A fixed procedure over the segment pack's rules gives a tier and a written reason for every point. There "
             "is no model in the score.",
     "record": [("deal.stage_changed", "scored")]},
    {"title": "The first touch is drafted", "stage": "scored", "nodes": ["agents", "tools", "llm", "lessons", "skills"],
     "edges": ["orchestrator-agents"],
     "text": "A model drafts one short personalised line from cited facts. Lessons choose the angle. A code check rejects "
             "any number, name or date that is not in the evidence, then a model verifier must support every claim.",
     "record": [("lesson.cited", None)]},
    {"title": "The policy engine decides", "stage": "scored", "nodes": ["agents", "policy"], "edges": ["agents-policy"],
     "text": "The agent's proposal is only a proposal. The policy engine looks the action up on its allowlist, applies "
             "the hard blocks, then picks a route: send, veto window, approval, or draft only.",
     "record": [("policy.decided", None)]},
    {"title": "A person or a timer approves", "stage": "scored",
     "nodes": ["policy", "veto", "approval"], "edges": ["policy-veto", "policy-approval"],
     "text": "Safe actions go straight on. Top-tier first touches wait in a veto window. Anything else that needs a "
             "person waits in Slack or the dashboard. A hard block never reaches a button.",
     "record": [("action.approved", None)]},
    {"title": "The send gate re-checks everything", "stage": "scored",
     "nodes": ["policy", "gate", "approval", "veto"], "edges": ["policy-gate", "veto-gate", "approval-gate"],
     "text": "The gate ignores any flag the caller passes. It re-reads the approval from the database and checks the kill "
             "switch, the recipient allowlist, the send window and the caps. A demo bypass is allowed only in demo "
             "mode and writes an audit row each time.",
     "record": [("send_window.bypassed", None), ("domain_cap.bypassed", None), ("gate.held", None)]},
    {"title": "The outbox sends it", "stage": "outreach", "nodes": ["gate", "outbox", "gmail", "hubspot"],
     "edges": ["gate-outbox", "outbox-gmail", "outbox-hubspot"],
     "text": "The email goes out through the outbox with a unique key, and is logged on the contact in the CRM. Before "
             "any retry the outbox asks the provider whether it already happened.",
     "record": [("action.sent", None), ("deal.stage_changed", "outreach")]},
    {"title": "A reply arrives", "stage": "outreach", "nodes": ["pollers", "gmail", "events"],
     "edges": ["pollers-events"],
     "text": "The poller finds the reply and matches it to the deal by thread. Reply text is untrusted: it is wrapped, "
             "scanned for instructions and never obeyed.",
     "record": [("inbound.matched", None)]},
    {"title": "The reply is classified", "stage": "replied", "nodes": ["orchestrator", "agents", "llm"],
     "edges": ["orchestrator-agents"],
     "text": "A small model labels the new text of the reply and quotes the words that justify the label. Code checks "
             "the quote really is in the reply.",
     "record": [("reply.classified", None), ("deal.stage_changed", "replied")]},
    {"title": "Meeting times are offered and booked", "stage": "meeting", "nodes": ["agents", "calendar", "skills", "outbox"],
     "edges": ["outbox-gmail"],
     "text": "Slots come from the calendar, in the prospect's own time zone, on the weekday they asked for. A slot is "
             "booked only when times were actually offered and the reply picks one.",
     "record": [("slots.offered", None), ("meeting.booked", None), ("deal.stage_changed", "meeting")]},
    {"title": "The call is matched and qualified", "stage": "qualified", "nodes": ["pollers", "fireflies", "agents", "orchestrator"],
     "edges": ["pollers-events", "orchestrator-agents"],
     "text": "A finished recording is matched to a deal only on evidence (participant email, title, meeting time). Each "
             "field the model extracts must come with a quote that really is in the transcript, or it is dropped.",
     "record": [("call.matched", None), ("deal.stage_changed", "qualified")]},
    {"title": "The IO is built and validated", "stage": "io_approval", "nodes": ["agents", "skills", "packs", "policy"],
     "edges": ["orchestrator-agents", "agents-policy"],
     "text": "A rules-based agent builds the order from the call and the segment's sample rate card. Code validates "
             "budget, term, discount and signer. A discount over the maximum is a hard block with no override.",
     "record": [("deal.io_terms_set", None), ("deal.stage_changed", "io_approval")]},
    {"title": "It goes out for signature (test mode)", "stage": "out_for_signature",
     "nodes": ["gate", "outbox", "dropbox", "webhooks", "events"], "edges": ["gate-outbox", "outbox-dropbox", "webhooks-events"],
     "text": "The signature request goes only to the allowlisted signer, in Dropbox Sign's test mode. Its callbacks come "
             "back as signed events and move the deal when it is signed.",
     "record": [("signature.sent", None), ("signature.signed", None), ("deal.signed", None)]},
    {"title": "Handoff, and everything is on the record", "stage": "handoff",
     "nodes": ["outbox", "slack", "hubspot", "audit", "postgres"], "edges": ["outbox-slack", "outbox-audit", "outbox-hubspot"],
     "text": "A closed-won summary goes to Customer Success in Slack and the CRM. Every decision above is in the "
             "append-only audit log, written in the same transaction as the change.",
     "record": [("deal.stage_changed", "closed_won"), ("deal.stage_changed", "handoff")]},
]


def _note(event: str, detail: Mapping[str, Any]) -> str:
    """A short, safe description of one audit row: only whitelisted fields, never free text from outside."""
    if event == "deal.stage_changed":
        return f"{detail.get('from', '?')} -> {detail.get('to', '?')}"
    if event == "reply.classified":
        return f"label: {detail.get('label', '?')}"
    if event == "policy.decided":
        return f"{detail.get('outcome', '?')}: {'; '.join(detail.get('reasons', [])[:1])}"
    if event == "enrichment.completed":
        return f"mode {detail.get('mode', '?')}, {len(detail.get('accepted', []))} page(s) accepted"
    if event == "action.sent":
        return f"{detail.get('type', 'action')} sent"
    if event in ("send_window.bypassed", "domain_cap.bypassed", "gate.held"):
        return str(detail.get("reason", ""))[:100]
    if event == "lesson.cited":
        return f"angle: {detail.get('angle', '?')}"
    return ""


def _recorded(rows: Sequence[Mapping[str, Any]], steps: Sequence[Mapping[str, Any]]) -> list[list[dict[str, str]]]:
    out: list[list[dict[str, str]]] = []
    for step in steps:
        lines = []
        for event, to in step["record"]:
            row = next((r for r in rows if r["event"] == event and (to is None or (r["detail"] or {}).get("to") == to)), None)
            if row:
                lines.append({"at": row["at"].isoformat(), "event": event,
                              "note": data.mask(_note(event, row["detail"] or {}))})
        out.append(lines)
    return out


# Fixed one-line captions (what each box is made of); numbers and modes are computed in _subtitles().
CAPTIONS = {
    "cli": "seed, run, draft", "webhooks": "Slack, Dropbox Sign", "events": "unique key, leases, backoff, dead letters",
    "orchestrator": "job handlers, stage machine, exit criteria", "llm": "Sonnet, Haiku (default models)",
    "lessons": "tag + keyword", "policy": "allowlist, autonomy mode, hard blocks", "veto": "Cancel button in Slack",
    "approval": "Slack, dashboard", "draft": "LinkedIn, no provider",
    "gate": "kill switch, DB re-read, allowlist, window, caps", "outbox": "unique key per action",
    "audit": "append-only, every decision", "postgres": "all state: deals, queue, outbox, audit",
}


def _subtitles(schedules: Sequence[Schedule], pack_count: int, integrations: Mapping[str, Mapping[str, str]],
               research_mode: str) -> dict[str, str]:
    kinds = [a.kind for a in AGENTS.values()]
    every = {s.event_type: s.every_seconds for s in schedules}
    skill_count = len([p for p in SKILLS_DIR.glob("*.py") if p.name != "__init__.py"])
    return {
        **CAPTIONS,
        "scheduler": f"{len(schedules)} recurring events",
        "pollers": f"Gmail {every.get('email.poll', 0)} s, Fireflies {every.get('calls.poll', 0)} s",
        "agents": f"{len(kinds)}: {kinds.count('deterministic')} rules, {kinds.count('judgement')} model-judged",
        "skills": f"{skill_count} modules",
        "tools": f"{len(REGISTRY)}, scoped per agent",
        "packs": f"{pack_count} packs, YAML",
        "research": research_mode,
        **{k: v.get("mode_label", "") for k, v in integrations.items()},
    }


def walkthrough(conn: psycopg.Connection, deal_id: str | None, *, schedules: Sequence[Schedule], pack_count: int,
                integrations: Mapping[str, Mapping[str, str]], research_mode: str) -> dict[str, Any]:
    cur = conn.cursor(row_factory=dict_row)
    deals = cur.execute(
        "SELECT d.id::text AS id, co.name AS company, d.stage, max(l.at) AS last FROM deals d "
        "LEFT JOIN companies co ON co.id = d.company_id LEFT JOIN audit_log l ON l.deal_id = d.id "
        "GROUP BY d.id, co.name, d.stage ORDER BY (d.stage = 'handoff') DESC, max(l.at) DESC NULLS LAST LIMIT 40"
    ).fetchall()
    chosen = next((d for d in deals if d["id"] == deal_id), deals[0] if deals else None)
    rows: list[dict[str, Any]] = []
    if chosen:
        rows = cur.execute("SELECT at, event, detail FROM audit_log WHERE deal_id = %s ORDER BY at, id",
                           (UUID(chosen["id"]),)).fetchall()
        created = cur.execute("SELECT received_at AS at, type AS event, '{}'::jsonb AS detail FROM events "
                              "WHERE type = 'lead.created' AND idempotency_key LIKE %s",
                              (f"lead.created:{chosen['id']}%",)).fetchall()
        rows = sorted([*created, *rows], key=lambda r: r["at"])
    subtitles = _subtitles(schedules, pack_count, integrations, research_mode)
    nodes = [{"id": k, "x": v[0], "y": v[1], "w": v[2], "h": v[3], "title": v[4], "does": v[5],
              "sub": subtitles.get(k, ""), "world": k in ("gmail", "hubspot", "slack", "dropbox", "fireflies", "calendar")}
             for k, v in NODES.items()]
    recorded = _recorded(rows, STEPS)
    return {
        "deals": [{"id": d["id"], "company": d["company"] or "a deal", "stage": d["stage"]} for d in deals],
        "deal": {"id": chosen["id"], "company": chosen["company"] or "a deal", "stage": chosen["stage"]} if chosen else None,
        "nodes": nodes,
        "edges": [{"id": e[0], "from": e[1], "to": e[2], "label": e[3], "fromSide": e[4], "toSide": e[5]} for e in EDGES],
        "stages": maps_data.MAIN_PATH,
        "steps": [{"title": s["title"], "text": s["text"], "stage": s["stage"], "nodes": s["nodes"], "edges": s["edges"],
                   "recorded": recorded[i]} for i, s in enumerate(STEPS)],
    }
