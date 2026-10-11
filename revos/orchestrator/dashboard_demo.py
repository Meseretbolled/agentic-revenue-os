"""Demo rows for the dashboard. Nothing here sends anything: it inserts rows only (no jobs, no events), and
every contact is a `.invalid` address that no mailer can reach and the recipient allowlist refuses anyway.

Everything is marked: the company name ends in "(demo)", the deal's fields carry `demo: true` and the
audit rows are written by the actor `demo-seed`. Deal stages move through `transition_deal`, like any deal.
The audit log is append-only, so clearing removes the demo deals and their rows but leaves the audit trail."""

from datetime import timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from revos.domain.stages import DealStage
from revos.orchestrator.transitions import transition_deal
from revos.policy.audit import write_audit
from revos.ports.clock import Clock

ACTOR = "demo-seed"
PATH = [
    DealStage.ENRICHED,
    DealStage.SCORED,
    DealStage.OUTREACH,
    DealStage.REPLIED,
    DealStage.MEETING,
    DealStage.QUALIFIED,
    DealStage.PROPOSAL,
    DealStage.IO_APPROVAL,
    DealStage.OUT_FOR_SIGNATURE,
    DealStage.CLOSED_WON,
    DealStage.HANDOFF,
]

SCORE = """Tier {tier}: total {total}/100 (fit {fit}, intent {intent}, engagement {eng})
  [fit] +30 Operates {n} locations: strong multi-location scale
  [fit] +20 Multi-site operator: local teams need on-brand, compliant campaigns
  [fit] +15 Industry is in the target list
  [intent] +40 Asked for a demo
  [intent] +20 Visited the pricing page
  [engagement] +15 Replied to the first email"""

CALL = {
    "budget": (
        "about $40,000 a month across all locations",
        "Our budget is about $40,000 a month across all locations.",
    ),
    "timeline": ("within six weeks", "We want each running within six weeks."),
    "decision_maker": ("Dana Park, CMO", "Dana Park, our CMO, makes the call."),
}
TRANSCRIPT = (
    "Jo: Thanks for joining. Dana: Our budget is about $40,000 a month across all locations. "
    "We want each running within six weeks. Dana Park, our CMO, makes the call."
)

DEALS: list[dict[str, Any]] = [
    {
        "company": "Northwind Optical (demo)",
        "contact": "Priya Shah",
        "stage": DealStage.SCORED,
        "tier": "B",
        "total": 61,
        "fit": 70,
        "intent": 40,
        "eng": 0,
        "n": 22,
        "agent": "scoring",
    },
    {
        "company": "Lakeside Dental Group (demo)",
        "contact": "Tom Becker",
        "stage": DealStage.OUTREACH,
        "tier": "A",
        "total": 84,
        "fit": 100,
        "intent": 80,
        "eng": 10,
        "n": 64,
        "agent": "outreach",
        "mail": True,
        "veto": True,
    },
    {
        "company": "Summit Fitness Partners (demo)",
        "contact": "Ana Lopez",
        "stage": DealStage.REPLIED,
        "tier": "A",
        "total": 88,
        "fit": 100,
        "intent": 90,
        "eng": 25,
        "n": 41,
        "agent": "reply",
        "mail": True,
        "reply": True,
        "approval": True,
    },
    {
        "company": "Brightside Tutoring (demo)",
        "contact": "Lee Tran",
        "stage": DealStage.MEETING,
        "tier": "B",
        "total": 66,
        "fit": 75,
        "intent": 55,
        "eng": 20,
        "n": 18,
        "agent": "meeting",
        "mail": True,
        "reply": True,
    },
    {
        "company": "Harbor Auto Care (demo)",
        "contact": "Sam Ortiz",
        "stage": DealStage.OUT_FOR_SIGNATURE,
        "tier": "A",
        "total": 91,
        "fit": 100,
        "intent": 95,
        "eng": 30,
        "n": 96,
        "agent": "contract_io",
        "mail": True,
        "reply": True,
        "call": True,
        "io": True,
        "lesson": True,
        "blocked": True,
    },
    {
        "company": "Pine Valley Pharmacies (demo)",
        "contact": "Maya Cole",
        "stage": DealStage.HANDOFF,
        "tier": "A",
        "total": 86,
        "fit": 100,
        "intent": 85,
        "eng": 20,
        "n": 54,
        "agent": "contract_io",
        "mail": True,
        "reply": True,
        "call": True,
        "io": True,
    },
    {
        "company": "Quick Lube Collective (demo)",
        "contact": "Raj Patel",
        "stage": DealStage.CLOSED_LOST,
        "tier": "C",
        "total": 31,
        "fit": 40,
        "intent": 20,
        "eng": 0,
        "n": 7,
        "agent": "reply",
        "mail": True,
        "reply": True,
    },
]

ITEMS = [
    {"id": "creative_automation", "quantity": 2},
    {"id": "multi_location", "quantity": 85},
    {"id": "measurement", "quantity": 1},
    {"id": "campaign_media", "quantity": 30100},
]
def io_lines(rate_card: Any = None) -> list[dict[str, Any]]:
    """The demo IO's lines, named and priced from the pack's rate card when one is given."""
    catalogue = {i.id: i for i in rate_card.items} if rate_card is not None else {}
    lines = []
    for item in ITEMS:
        known = catalogue.get(item["id"])
        price = known.price if known else 1
        name, unit = (known.name, known.unit) if known else (item["id"], "per month")
        lines.append({"name": name, "unit": unit, "quantity": item["quantity"], "unit_price": price,
                      "amount": price * item["quantity"]})
    return lines


def has_demo(conn: psycopg.Connection) -> bool:
    return conn.execute("SELECT 1 FROM deals WHERE fields->>'demo' = 'true' LIMIT 1").fetchone() is not None


def seed_demo(conn: psycopg.Connection, clock: Clock, segment_id: str, rate_card: Any = None) -> int:
    """Insert the demo deals once. Returns how many were created (0 when they are already there)."""
    if has_demo(conn):
        return 0
    now = clock.now()
    lessons = [
        conn.execute(
            "INSERT INTO lessons (segment_id, text, evidence, tags, confidence, source, uses) "
            "VALUES (%s, %s, %s, %s, %s, "
            "'call', 1) RETURNING id",
            (segment_id, text, quote, tags, conf),
        ).fetchone()[0]
        for text, quote, tags, conf in (
            (
                "CPM objection: lead with return on spend, not the price.",
                "Local video CPMs have been climbing.",
                ["pricing", "cpm"],
                0.7,
            ),
            (
                "Attribution gaps: lead with measurement and attribution.",
                "I cannot tell which campaigns work.",
                ["attribution"],
                0.6,
            ),
        )
    ]
    with conn.transaction():
        for i, spec in enumerate(DEALS):
            company = conn.execute(
                "INSERT INTO companies (segment_id, name, domain, data) VALUES (%s, %s, %s, %s) RETURNING id",
                (segment_id, spec["company"], f"demo-{i}.invalid", Jsonb({"demo": True})),
            ).fetchone()[0]
            contact = conn.execute(
                "INSERT INTO contacts (company_id, email, full_name, timezone) VALUES "
                "(%s, %s, %s, 'America/Chicago') RETURNING id",
                (company, f"demo.contact{i}@demo-seed.invalid", spec["contact"]),
            ).fetchone()[0]
            fields: dict[str, Any] = {"demo": True}
            if spec.get("call"):
                for name, (value, quote) in CALL.items():
                    fields[name], fields[f"quote:{name}"] = value, quote
            if spec.get("io"):
                fields.update(
                    io_items=ITEMS,
                    io_package="scale",
                    io_terms_source="auto",
                    io_start_date="2026-10-26",
                    io_end_date="2027-01-26",
                    discount_pct=0,
                )
            deal = conn.execute(
                "INSERT INTO deals (segment_id, company_id, contact_id, tier, fit_score, intent_score, "
                "engagement_score, total_score, score_reason, fields, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s) RETURNING id",
                (
                    segment_id,
                    company,
                    contact,
                    spec["tier"],
                    spec["fit"],
                    spec["intent"],
                    spec["eng"],
                    spec["total"],
                    SCORE.format(
                        tier=spec["tier"],
                        total=spec["total"],
                        fit=spec["fit"],
                        intent=spec["intent"],
                        eng=spec["eng"],
                        n=spec["n"],
                    ),
                    Jsonb(fields),
                    now - timedelta(days=5 - min(i, 4), hours=3 * i),
                ),
            ).fetchone()[0]
            for stage in (
                PATH[: PATH.index(spec["stage"]) + 1]
                if spec["stage"] in PATH
                else (*PATH[:3], DealStage.CLOSED_LOST)
            ):
                transition_deal(conn, deal, stage, actor=ACTOR, reason="demo data")
            conn.execute(
                "INSERT INTO agent_runs (idempotency_key, deal_id, agent, step, runner, status, input, "
                "finished_at) VALUES (%s, %s, %s, 'demo', 'rules', 'completed', '{}', now())",
                (f"demo:{deal}:{spec['agent']}", deal, spec["agent"]),
            )
            if spec.get("mail"):
                conn.execute(
                    "INSERT INTO messages (deal_id, contact_id, direction, message_id, subject, body) VALUES "
                    "(%s, %s, 'outbound', %s, %s, %s)",
                    (
                        deal,
                        contact,
                        f"<demo-out-{i}@demo-seed.invalid>",
                        f"Scaling local campaigns at {spec['company']}",
                        "Hi,\n\nDemo draft written for the dashboard. Not a real email and never sent.\n",
                    ),
                )
            if spec.get("reply"):
                conn.execute(
                    "INSERT INTO messages (deal_id, contact_id, direction, message_id, subject, body, "
                    "classification) VALUES (%s, %s, 'inbound', %s, %s, %s, 'interested')",
                    (
                        deal,
                        contact,
                        f"<demo-in-{i}@demo-seed.invalid>",
                        "Re: demo thread",
                        "Yes, interested, can we talk Thursday?",
                    ),
                )
            if spec.get("call"):
                conn.execute(
                    "INSERT INTO transcripts (deal_id, external_id, body) VALUES (%s, %s, %s)",
                    (deal, f"demo-call-{i}", TRANSCRIPT),
                )
            if spec.get("io"):
                conn.execute(
                    "INSERT INTO documents (deal_id, kind, version, body, validation) VALUES (%s, 'io', 1, "
                    "'(demo IO)', %s)",
                    (
                        deal,
                        Jsonb(
                            {
                                "ok": True,
                                "problems": [],
                                "total": 120000.0,
                                "line_items": io_lines(rate_card),
                                "monthly_subtotal": 40000.0,
                                "months": 3,
                                "term_subtotal": 120000.0,
                                "package": "scale",
                            }
                        ),
                    ),
                )
            if spec.get("lesson"):
                action = _action(conn, deal, contact, "follow_up", "approved", now, subject="Demo follow-up")
                for lesson in lessons:
                    conn.execute(
                        "INSERT INTO lesson_uses (lesson_id, deal_id, action_id, outcome) VALUES "
                        "(%s, %s, %s, 'reply_positive')",
                        (lesson, deal, action),
                    )
            if spec.get("veto"):
                _action(
                    conn,
                    deal,
                    contact,
                    "follow_up",
                    "pending",
                    now,
                    subject="Follow-up for Lakeside (demo)",
                    veto=now + timedelta(minutes=15),
                )
            if spec.get("approval"):
                _action(
                    conn, deal, contact, "follow_up", "awaiting_approval", now, subject="Meeting times (demo)"
                )
            if spec.get("blocked"):
                blocked = _action(
                    conn,
                    deal,
                    contact,
                    "send_io",
                    "cancelled",
                    now,
                    subject="IO for Harbor Auto Care",
                    decided_by="policy",
                )
                write_audit(
                    conn,
                    ACTOR,
                    "policy.decided",
                    deal,
                    {
                        "action_id": str(blocked),
                        "action_type": "send_io",
                        "outcome": "blocked",
                        "reasons": ["discount_over_maximum"],
                        "held": False,
                    },
                )
    conn.execute(
        "INSERT INTO jobs (type, status, attempts, last_error) VALUES ('calls.poll', 'dead', 5, %s) "
        "RETURNING id",
        ("FirefliesUnavailable: network (demo dead letter)",),
    )
    job = conn.execute("SELECT max(id) FROM jobs").fetchone()[0]
    conn.execute(
        "INSERT INTO dead_letters (job_id, type, error) VALUES (%s, 'calls.poll', %s)",
        (job, "FirefliesUnavailable: Fireflies list transcripts unavailable: network (demo dead letter)"),
    )
    return len(DEALS)


def _action(
    conn: psycopg.Connection,
    deal,
    contact,
    kind: str,
    status: str,
    now,
    *,
    subject: str,
    veto=None,
    decided_by: str | None = None,
):
    decided = status in ("approved", "cancelled")
    return conn.execute(
        "INSERT INTO actions (idempotency_key, deal_id, contact_id, type, autonomy_level, status, decision, "
        "decision_by, decided_at, veto_until, payload) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "RETURNING id",
        (
            f"demo:{deal}:{kind}:{status}",
            deal,
            contact,
            kind,
            "auto_with_veto" if veto else "needs_approval",
            status,
            "approved" if status == "approved" else "rejected" if status == "cancelled" else None,
            (decided_by or "demo-seed") if decided else None,
            now if decided else None,
            veto,
            Jsonb({"subject": subject, "demo": True}),
        ),
    ).fetchone()[0]


def clear_demo(conn: psycopg.Connection) -> int:
    """Remove the demo deals and everything hanging off them (the append-only audit log stays)."""
    ids = [r[0] for r in conn.execute("SELECT id FROM deals WHERE fields->>'demo' = 'true'").fetchall()]
    if not ids:
        return 0
    with conn.transaction():
        actions = [r[0] for r in conn.execute("SELECT id FROM actions WHERE deal_id = ANY(%s)", (ids,))]
        for table, column in (
            ("lesson_uses", "deal_id"),
            ("documents", "deal_id"),
            ("transcripts", "deal_id"),
            ("messages", "deal_id"),
        ):
            conn.execute(f"DELETE FROM {table} WHERE {column} = ANY(%s)", (ids,))
        conn.execute("DELETE FROM approvals WHERE action_id = ANY(%s)", (actions,))
        conn.execute("DELETE FROM actions WHERE deal_id = ANY(%s)", (ids,))
        runs = [r[0] for r in conn.execute("SELECT id FROM agent_runs WHERE deal_id = ANY(%s)", (ids,))]
        for table in ("agent_proposals", "agent_steps"):
            conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (runs,))
        conn.execute("DELETE FROM agent_runs WHERE deal_id = ANY(%s)", (ids,))
        contacts = [r[0] for r in conn.execute("SELECT contact_id FROM deals WHERE id = ANY(%s)", (ids,))]
        companies = [r[0] for r in conn.execute("SELECT company_id FROM deals WHERE id = ANY(%s)", (ids,))]
        conn.execute("DELETE FROM deals WHERE id = ANY(%s)", (ids,))
        conn.execute("DELETE FROM contacts WHERE id = ANY(%s)", (contacts,))
        conn.execute("DELETE FROM companies WHERE id = ANY(%s)", (companies,))
        conn.execute("DELETE FROM dead_letters WHERE error LIKE '%%(demo dead letter)%%'")
        conn.execute("DELETE FROM jobs WHERE last_error LIKE '%%(demo dead letter)%%'")
        conn.execute(
            "DELETE FROM lessons WHERE id NOT IN (SELECT lesson_id FROM lesson_uses) AND "
            "evidence IN ('Local video CPMs have been climbing.', 'I cannot tell which campaigns work.')"
        )
    return len(ids)
