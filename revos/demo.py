"""`make demo`: one fictional lead from arrival to handoff, offline, narrated step by step.

Everything external is a mock (mail, CRM, calendar, e-signature, chat, research) and the clock is
simulated. The agents run on whichever runner LLM_MODE selects, and every step says which.
Nothing in this module talks to a real service unless LLM_MODE=live.
"""

import json
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from revos.adapters.clock import FakeClock
from revos.adapters.mock import build_mock_world
from revos.agents.types import Identity
from revos.composition import build_runner
from revos.db import connect_read_only, database_url, migrate
from revos.orchestrator.pipeline import Orchestrator
from revos.ports.mail import InboundEmail
from revos.runtime.worker import Worker
from revos.seed import demo_address
from revos.segments import load_all
from revos.skills.slots import slot_text

ROOT = Path(__file__).resolve().parent.parent
CONTACT = demo_address("brightside")      # a plus-address of the prospect inbox, never .example
DEMO_DB = "revos_demo"
START = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)          # a Monday, 09:00 UTC


@dataclass
class DemoReport:
    runner: str = ""
    stages: list[str] = field(default_factory=list)
    angle_before: str = ""
    angle_after: str = ""
    lesson_id: int | None = None
    emails: int = 0
    invites: int = 0
    handoffs: int = 0
    final_stage: str = ""
    facts: dict[str, Any] = field(default_factory=dict)


def prepare_demo_database(base_url: str | None = None) -> str:
    """A fresh, migrated database just for the demo, so it can be run again and again."""
    url = base_url or database_url()
    admin = psycopg.connect(url.rsplit("/", 1)[0] + "/postgres", autocommit=True)
    with admin:
        admin.execute(f"DROP DATABASE IF EXISTS {DEMO_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {DEMO_DB}")
    demo_url = url.rsplit("/", 1)[0] + f"/{DEMO_DB}"
    with psycopg.connect(demo_url, autocommit=True) as conn:
        migrate(conn)
    return demo_url


class Narrator:
    """Prints each step, then what the system recorded while it ran."""

    def __init__(self, conn: psycopg.Connection, out: Callable[[str], None]) -> None:
        self.conn, self.out = conn, out
        self.audit_seen = 0
        self.runs_seen: set[Any] = set()
        self.n = 0

    def step(self, title: str) -> None:
        self.n += 1
        self.out(f"\n=== Step {self.n}: {title}")

    def say(self, text: str) -> None:
        for line in str(text).splitlines():
            self.out(f"    {line}")

    def record(self) -> None:
        cur = self.conn.cursor(row_factory=dict_row)
        runs = cur.execute("SELECT id, agent, step, runner, status, human_reason FROM agent_runs "
                           "ORDER BY started_at").fetchall()
        kinds = {"enrichment": "deterministic", "scoring": "deterministic",
                 "qualification": "deterministic", "contract_io": "deterministic",
                 "tracking": "deterministic"}
        for run in runs:
            if run["id"] not in self.runs_seen and run["status"] != "running":
                self.runs_seen.add(run["id"])
                self.out(f"  agent {run['agent']:<14} [{kinds.get(run['agent'], 'judgement'):<13}] "
                         f"runner={run['runner']:<5} status={run['status']}"
                         + (f"  ({run['human_reason']})" if run["human_reason"] else ""))
        rows = cur.execute("SELECT id, event, detail FROM audit_log WHERE id > %s ORDER BY id",
                           (self.audit_seen,)).fetchall()
        for row in rows:
            self.audit_seen = row["id"]
            line = self._audit_line(row["event"], row["detail"])
            if line:
                self.out(f"  audit  {line}")

    @staticmethod
    def _audit_line(event: str, d: dict[str, Any]) -> str | None:
        if event in {"agent.run_started", "agent.run_completed", "agent.run_resumed"}:
            return None                                   # shown as the agent lines above
        if event == "policy.decided":
            why = "; ".join(d["reasons"][:2])
            return f"policy.decided      {d['action_type']} -> {d['outcome']} ({why})"
        if event == "deal.stage_changed":
            return f"deal.stage_changed  {d['from']} -> {d['to']}  [{d['reason']}]"
        if event == "lesson.cited":
            return (f"lesson.cited        lessons {d['lesson_ids']} shaped {d['action_type']} "
                    f"(angle {d['angle']})")
        if event == "lesson.outcome":
            moved = ", ".join(f"L{x['id']} -> confidence {x['confidence']:.2f}" for x in d["lessons"])
            return f"lesson.outcome      {moved} ({d['outcome']})"
        if event == "action.sent":
            return f"action.sent         {d['type']} -> {d['provider_id']}"
        text = json.dumps(d, default=str)
        return f"{event:<20} {text[:150]}"


def run_demo(url: str, out: Callable[[str], None] = print) -> DemoReport:
    pack = next(p for p in load_all(ROOT / "packs").values() if p.meta.demo)
    clock = FakeClock(START)
    world = build_mock_world()
    report = DemoReport()
    with psycopg.connect(url, autocommit=True) as conn, connect_read_only(url) as ro:
        runner = build_runner(conn)
        report.runner = runner.name
        orch = Orchestrator(
            conn, ro, clock=clock, packs={pack.id: pack}, runner=runner, research=world.research,
            provider=world.router, identity=Identity(), demo_mode=True,
            allowed_recipients=frozenset({CONTACT}), calendar=world.calendar,
            transcripts=world.transcripts, esign=world.esign)
        worker = Worker(orch.handlers(), clock, rng=random.Random(7))
        n = Narrator(conn, out)

        def drain() -> None:
            while worker.run_once(conn):
                pass

        def stage() -> str:
            return conn.execute("SELECT stage FROM deals WHERE id = %s", (deal_id,)).fetchone()[0]

        def reply(text: str, mid: str) -> Any:
            return orch.handle_reply(deal_id, InboundEmail(
                f"<{mid}@brightside-optical.example>", CONTACT, "me@sender.example", "Re: hello",
                text, clock.now()))

        out("OFFLINE DEMO. Real: the code, Postgres, the policy engine, the outbox, the audit log.")
        out("Mocked: mail, CRM, calendar, e-signature (test mode), chat, web research. "
            "The clock is simulated.")
        out(f"Runner for every agent: {runner.name} "
            f"({'deterministic rules, fake model' if runner.name == 'rules' else 'Claude Agent SDK'}).")
        out("The lead is fictional; the only address ever mailed is the one on the contact row.")

        # 1 ------------------------------------------------------------------------------------
        n.step("a fictional lead arrives, is enriched, scored and gets a first touch")
        conn.execute("INSERT INTO segments (id, pack_version) VALUES (%s, %s)", (pack.id, pack.meta.version))
        company = conn.execute("INSERT INTO companies (segment_id, name, domain) VALUES (%s, %s, %s) "
                               "RETURNING id", (pack.id, "Brightside Optical Group",
                                                "brightside-optical.example")).fetchone()[0]
        contact = conn.execute("INSERT INTO contacts (company_id, email, full_name, timezone) VALUES "
                               "(%s, %s, 'Sam Lee', 'UTC') RETURNING id", (company, CONTACT)).fetchone()[0]
        deal_id = conn.execute("INSERT INTO deals (segment_id, company_id, contact_id) VALUES (%s, %s, %s) "
                               "RETURNING id", (pack.id, company, contact)).fetchone()[0]
        first = orch.handle_lead_created(deal_id)
        sent = orch.send_action(first.action_id)
        mail = world.mailer.sent[-1]
        n.say(f"first touch {sent.status}: to {mail.to}\nsubject: {mail.subject}\n{mail.body}")
        n.record()

        # 2 ------------------------------------------------------------------------------------
        clock.advance(20 * 60)
        n.step("the prospect is interested: meeting times are offered and a call brief is written")
        result = reply("Sounds good, let's talk next week", "r1")
        drain()
        n.say(f"reply routed as: {result.label}; times offered:")
        n.say(world.mailer.sent[-1].body.split("Times that work on our side:")[1].split("\n\n")[0].strip())
        n.record()

        # 3 ------------------------------------------------------------------------------------
        n.step("the follow-up angle BEFORE the call (a preview, not sent)")
        before = orch.run_agent(deal_id, "outreach", "demo.angle.before", input_={"kind": "follow_up"})
        before_payload = before.proposals[0]["payload"]
        report.angle_before = before_payload["angle"]
        n.say(f"angle: {before_payload['angle']}   lessons cited: {before_payload['lesson_ids'] or 'none'}")
        n.record()

        # 4 ------------------------------------------------------------------------------------
        clock.set(datetime(2026, 10, 13, 9, 0, tzinfo=UTC))
        n.step("the prospect accepts one of the times: meeting booked, invite attached")
        offered = [datetime.fromisoformat(s) for s in
                   conn.execute("SELECT fields->'offered_slots' FROM deals").fetchone()[0]]
        accept = reply(f"{offered[0]:%A %d %B} at {offered[0]:%H:%M} works for me", "r2")
        drain()
        invite = world.mailer.sent[-1].attachments[0]
        n.say(f"reply routed as: {accept.label}; booked: {slot_text(offered[0])}")
        n.say(f"confirmation to {world.mailer.sent[-1].to} with {invite.filename} "
              f"({[x for x in invite.content.splitlines() if x.startswith('ATTENDEE')][0]})")
        n.record()

        # 5 ------------------------------------------------------------------------------------
        n.step("the call: transcript to deal fields (each with its quote) and a lesson")
        orch.handle_transcript(deal_id, "call-003")
        fields = conn.execute("SELECT fields FROM deals").fetchone()[0]
        for name in ("budget", "timeline", "needs", "objections"):
            n.say(f"{name}: {fields[name]}\n   quote: {fields['quote:' + name]}")
        n.say(f"decision maker: {fields['decision_maker_name']} ({fields['decision_maker_role']})   "
              f"signer: {fields['signer_name']} ({fields['signer_role']})")
        lessons = conn.execute("SELECT id, text, tags, confidence, source FROM lessons "
                               "ORDER BY id").fetchall()
        for lid, text, tags, conf, source in lessons:
            n.say(f"lesson L{lid} [{source}] tags={tags} confidence={float(conf):.2f}: "
                  f"{text}")
        n.record()

        # 6 ------------------------------------------------------------------------------------
        n.step("the follow-up angle AFTER the call: the lesson changes it")
        after = orch.run_agent(deal_id, "outreach", "demo.angle.after", input_={"kind": "follow_up"})
        after_payload = after.proposals[0]["payload"]
        report.angle_after = after_payload["angle"]
        report.lesson_id = (after_payload["lesson_ids"] or [None])[0]
        n.say(f"angle WITHOUT the lesson: {report.angle_before}")
        n.say(f"angle WITH the lesson:    {report.angle_after}   (cites lesson L{report.lesson_id})")
        applied = orch.apply_run(after, deal_id)
        orch.send_action(applied[0]["action_id"])
        n.say(f"sent follow-up:\n{world.mailer.sent[-1].body}")
        n.record()

        # 7 ------------------------------------------------------------------------------------
        n.step("the prospect replies positively: the lesson that was used gains confidence")
        reply("This is helpful, I am interested in seeing more", "r3")
        n.say("confidence of L{}: {:.2f}".format(
            report.lesson_id, float(conn.execute("SELECT confidence FROM lessons WHERE id = %s",
                                                 (report.lesson_id,)).fetchone()[0])))
        n.record()

        # 8 ------------------------------------------------------------------------------------
        n.step("the qualification gate")
        gate = orch.run_qualification_gate(deal_id)
        n.say(f"gate: {gate.status}; deal is now {stage()}")
        n.record()

        # 9 ------------------------------------------------------------------------------------
        clock.set(datetime(2026, 10, 14, 9, 0, tzinfo=UTC))
        n.step("the IO: a discount over the maximum is blocked until a manager approves")
        items = [{"id": "multi_location", "quantity": 120}, {"id": "creative_automation", "quantity": 3}]
        orch.set_io_terms(deal_id, items=items, start="2026-11-02", end="2027-02-01", discount_pct=40,
                          actor="deal-desk")
        blocked = orch.handle_io(deal_id)
        n.say(f"IO result: {blocked.status}: {blocked.reason}")
        orch.approve_discount(deal_id, "regional-manager", 40, "strategic account")
        n.say("a manager approves 40% for this deal, then the IO is rebuilt")
        ready = orch.handle_io(deal_id)
        drain()
        n.say(f"IO result: {ready.status} (policy decision: {ready.decision}); deal is now {stage()}")
        request = world.esign.requests[-1]
        n.say(f"sent for signature in TEST MODE to {request.signer_email} for signer {request.signer_name}")
        n.say(conn.execute("SELECT body FROM documents ORDER BY version DESC LIMIT 1").fetchone()[0])
        n.record()

        # 10 -----------------------------------------------------------------------------------
        clock.set(datetime(2026, 10, 15, 9, 0, tzinfo=UTC))
        n.step("the signature arrives: verified, applied once, replay ignored; deal closes and is handed off")
        envelope = conn.execute("SELECT fields->>'envelope_id' FROM deals").fetchone()[0]
        world.esign.simulate_signed(envelope)
        body = world.esign.signed_event(envelope)
        forged = json.loads(json.dumps(body))
        forged["event"]["event_hash"] = "0" * 64
        n.say(f"forged webhook: {orch.handle_signature_webhook(forged)}")
        n.say(f"real webhook:   {orch.handle_signature_webhook(body)}")
        drain()
        n.say(f"same webhook again: {orch.handle_signature_webhook(body)}")
        drain()
        post = world.chat.posts[0]
        n.say(f"handoff posted to {post.channel} (once):\n{post.text}")
        n.record()

        # summary ------------------------------------------------------------------------------
        report.stages = [r[0] for r in conn.execute(
            "SELECT detail->>'to' FROM audit_log WHERE event = 'deal.stage_changed' ORDER BY id")]
        report.final_stage = stage()
        report.emails = len(world.mailer.sent)
        report.invites = len(world.calendar.invites)
        report.handoffs = len(world.chat.posts)
        report.facts = {"audit_rows": conn.execute("SELECT count(*) FROM audit_log").fetchone()[0],
                        "runs": conn.execute("SELECT count(*) FROM agent_runs").fetchone()[0]}
        out("\n=== Summary")
        out(f"  stages:   {' -> '.join(['new', *report.stages])}")
        out(f"  runner:   {report.runner} for all {report.facts['runs']} agent runs")
        out(f"  angle:    {report.angle_before} (no lesson) vs {report.angle_after} "
            f"(lesson L{report.lesson_id})")
        out(f"  sent:     {report.emails} emails, {report.invites} calendar invite, "
            f"{len(world.esign.requests)} IO for signature (test mode), {report.handoffs} handoff")
        out(f"  audit:    {report.facts['audit_rows']} entries; nothing sent without a recorded decision")
        out("  not real: no real email, CRM, calendar, e-signature, Slack or web request was made.")
    return report


def demo_main() -> None:
    url = prepare_demo_database()
    run_demo(url)

