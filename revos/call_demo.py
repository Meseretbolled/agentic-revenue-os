"""`revos call-demo`: a scripted call, fed through the same Transcripts port the real Fireflies adapter
implements, fully offline (mock adapters, a throwaway database)."""

import random
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import psycopg

from revos.adapters.clock import FakeClock
from revos.adapters.mock import build_mock_world
from revos.adapters.mock.fixtures import load
from revos.adapters.mock.slack import MockSlack
from revos.agents.types import Identity
from revos.composition import build_runner
from revos.db import connect_read_only
from revos.demo import ROOT, prepare_demo_database
from revos.domain.stages import DealStage
from revos.orchestrator.pipeline import Orchestrator
from revos.ports.transcripts import Sentence, Transcript
from revos.runtime.worker import Worker
from revos.seed import demo_address, load_profiles, seed_lead
from revos.segments import load_all
from revos.slack.notifier import SlackNotifier

START = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def scripted_transcript(contact_email: str) -> Transcript:
    item = next(t for t in load("transcripts")["transcripts"] if t["id"] == "call-demo-001")
    return Transcript(
        item["id"], item["title"], datetime.fromisoformat(item["date"]),
        tuple(p.replace("{contact_email}", contact_email) for p in item["participants"]),
        tuple(Sentence(i, s["speaker_name"], s["text"], float(i), float(i) + 1)
              for i, s in enumerate(item["sentences"])))


def run_call_demo(url: str, out: Callable[[str], None] = print) -> dict[str, Any]:
    pack = next(p for p in load_all(ROOT / "packs").values() if p.meta.demo)
    profile = load_profiles(pack.id)["hot"]
    clock = FakeClock(START)
    world = build_mock_world()
    slack = MockSlack()
    facts: dict[str, Any] = {}
    with psycopg.connect(url, autocommit=True) as conn, connect_read_only(url) as ro:
        email = demo_address("harborview")
        notifier = SlackNotifier(conn, slack, "#revops", clock, {pack.id: pack})
        orch = Orchestrator(
            conn, ro, clock=clock, packs={pack.id: pack}, runner=build_runner(conn), research=world.research,
            provider=world.router, identity=Identity(), demo_mode=True, allowed_recipients=frozenset({email}),
            calendar=world.calendar, transcripts=world.transcripts, esign=world.esign, notifier=notifier,
            crm_sync=True)
        worker = Worker(orch.handlers(), clock, rng=random.Random(3))
        out("OFFLINE CALL DEMO. Real: the code, Postgres, the quote check, the audit log.")
        out("Mocked: Fireflies (the same port), mail, CRM, Slack. The lead and the call are fictional.\n")

        deal_id, _ = seed_lead(
            conn, clock, segment_id=pack.id, pack_version=pack.meta.version, company=profile["company"],
            domain=profile["domain"], contact_name=profile["contact"], email=email,
            timezone=profile["timezone"],
            fields={**profile["fields"], "signer_email": demo_address(profile["signer_email_tag"])})
        orch.handle_lead_created(deal_id)
        for stage in (DealStage.OUTREACH, DealStage.REPLIED, DealStage.MEETING):
            orch.advance(deal_id, stage, "set-up for the call demo")
        out(f"1. A lead is in the {DealStage.MEETING.value} stage: {profile['company']} ({email}).")

        world.transcripts.add(scripted_transcript(email))
        titles = [t.title for t in world.transcripts.list_recent()]
        out(f"2. A call finishes in 'Fireflies'; the poll sees: {titles}")
        results = orch.poll_calls()
        out(f"3. The poll matched and processed it: {results}; polling again: {orch.poll_calls()} "
            "(nothing the second time).")
        for event in ("call.matched", "call.field_dropped", "call.qualification"):
            rows = conn.execute("SELECT detail FROM audit_log WHERE event = %s ORDER BY id", (event,))
            for (detail,) in rows:
                out(f"   audit {event}: {detail}")

        fields = conn.execute("SELECT fields, stage FROM deals WHERE id = %s", (deal_id,)).fetchone()
        out("\n4. What was extracted, each value with the quote it rests on (checked in code):")
        for name in ("needs", "budget", "timeline", "decision_maker", "signer", "objections", "competitors",
                     "next_steps"):
            if fields[0].get(name):
                out(f"   {name:<15}: \"{fields[0].get(f'quote:{name}', fields[0][name])}\"")
        out(f"   signer name     : {fields[0].get('signer_name')} (a NAME only)")
        out(f"   signer email    : {fields[0].get('signer_email')} (from the lead record, not the call)")
        out(f"\n5. Stage after the qualification gate: {fields[1]}")
        for lesson in conn.execute("SELECT id, text, tags, confidence FROM lessons ORDER BY id"):
            out(f"   lesson L{lesson[0]} (confidence {lesson[3]}, tags {lesson[2]}): {lesson[1][:110]}")

        worker_drained = 0
        while worker.run_once(conn):
            worker_drained += 1
        note = conn.execute("SELECT payload->>'text' FROM actions WHERE type = 'crm_note' AND "
                            "payload->>'text' LIKE 'Call summary%%' ORDER BY created_at LIMIT 1").fetchone()
        text = note[0] if note else ""
        out("\n6. CRM note written (mock CRM):\n" + "\n".join("   " + ln for ln in text.splitlines()))
        call_posts = [p for p in slack.posts if "Call processed" in p.text]
        out(f"\n7. Slack posts: {[p.text for p in call_posts]}")

        run = orch.run_agent(deal_id, "outreach", "outreach.2", input_={"kind": "follow_up"})
        payload = run.proposals[0]["payload"] if run.proposals else {}
        out(f"\n8. The next email cites lesson id(s) {payload.get('lesson_ids')} "
            f"(angle: {payload.get('angle')}):")
        out("\n".join("   " + ln for ln in (payload.get("body") or "").splitlines()))
        facts.update(stage=fields[1], lesson_ids=payload.get("lesson_ids"), results=results)
    return facts


def call_demo_main() -> None:
    run_call_demo(prepare_demo_database())
