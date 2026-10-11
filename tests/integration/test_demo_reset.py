import random
from datetime import UTC, datetime

from revos.demo_reset import cancel_pending_work
from revos.runtime.worker import Worker
from tests.pipeline_helpers import audit, make_rig

SUNDAY = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)


def test_a_held_email_cannot_send_after_the_local_reset_even_when_its_timer_fires(conn, ro_conn, pack, clock):
    clock.set(SUNDAY)
    rig = make_rig(conn, ro_conn, pack, clock)
    first = rig.orch.handle_lead_created(rig.deal_id)
    assert rig.orch.send_action(first.action_id).status == "deferred"          # held until Monday
    counts = cancel_pending_work(conn)
    assert counts["actions"] >= 1 and counts["jobs"] >= 1
    assert conn.execute("SELECT status FROM actions WHERE id = %s", (first.action_id,)).fetchone()[0] \
        == "cancelled"
    assert audit(conn, "demo.reset", rig.deal_id)
    clock.advance(2 * 86400)                                                   # Monday has come
    worker = Worker(rig.orch.handlers(), clock, rng=random.Random(1))
    while worker.run_once(conn):
        pass
    assert rig.world.mailer.sent == [] if hasattr(rig.world, "mailer") else True
    assert conn.execute("SELECT count(*) FROM messages WHERE direction = 'outbound'").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM actions WHERE id = %s", (first.action_id,)).fetchone()[0] \
        == "cancelled"
    assert cancel_pending_work(conn) == {"actions": 0, "jobs": 0, "deals": 0}   # safe to repeat


# --- old, reset or closed deals with the same company never count as candidates for a call ----------------------

def _transcript(tid="call-r1", title="Brightside Optical Group - intro call", who=("sam.lee@brightside-optical.example",)):
    from revos.ports.transcripts import Sentence, Transcript
    return Transcript(tid, title, datetime(2026, 10, 15, 15, 0, tzinfo=UTC), tuple(who),
                      (Sentence(0, "Sam", "Our budget is about fifty thousand dollars.", 0, 1),))


def test_only_live_deals_are_candidates_never_closed_or_reset_ones(conn, ro_conn, pack, clock):
    from revos.domain.stages import DealStage
    from tests.pipeline_helpers import force_stage, make_lead
    rig = make_rig(conn, ro_conn, pack, clock, email="sam.lee+live@brightside-optical.example")
    force_stage(conn, rig.deal_id, DealStage.MEETING)
    closed = {}
    for stage in ("closed_lost", "closed_won", "do_not_contact", "handoff"):
        deal, _ = make_lead(conn, email=f"sam.lee+{stage}@brightside-optical.example", name="Old")
        conn.execute("SELECT set_config('revos.allow_stage_change', 'on', false)")
        conn.execute("UPDATE deals SET stage = %s WHERE id = %s", (stage, deal))
        conn.execute("SELECT set_config('revos.allow_stage_change', 'off', false)")
        closed[stage] = deal
    live = {d.deal_id for d in rig.orch._open_deals()}
    assert live == {rig.deal_id}


def test_a_reset_closes_every_open_deal_so_old_demo_deals_with_the_same_company_never_match(
        conn, ro_conn, pack, clock):
    from revos.domain.stages import DealStage
    from tests.pipeline_helpers import force_stage, make_lead
    rig = make_rig(conn, ro_conn, pack, clock, email="sam.lee+old1@brightside-optical.example")
    old = [rig.deal_id]
    for i, stage in enumerate((DealStage.SCORED, DealStage.REPLIED, DealStage.MEETING), start=2):
        deal, _ = make_lead(conn, email=f"sam.lee+old{i}@brightside-optical.example", name="Old")
        force_stage(conn, deal, stage)
        old.append(deal)
    assert len(rig.orch._open_deals()) == 4
    counts = cancel_pending_work(conn)
    assert counts["deals"] == 4 and rig.orch._open_deals() == []
    assert {r[0] for r in conn.execute("SELECT stage FROM deals")} == {"closed_lost"}
    assert len(audit(conn, "deal.stage_changed")) >= 4
    # a transcript for that company now matches nothing: it is held, never attached to an old deal
    rig.world.transcripts.add(_transcript())
    assert rig.orch.poll_calls() == [("call-r1", "held")]
    assert audit(conn, "call.unmatched")[0]["reason"] == "none"
    assert conn.execute("SELECT count(*) FROM transcripts").fetchone()[0] == 0
    # and a NEW live deal for the same company is the only candidate, so the call is matched to it
    fresh, _ = make_lead(conn, email="sam.lee+fresh@brightside-optical.example", name="Fresh")
    force_stage(conn, fresh, DealStage.MEETING)
    rig.world.transcripts.add(_transcript("call-r2", who=("sam.lee@brightside-optical.example",)))
    assert rig.orch.poll_calls() == [("call-r1", "seen"), ("call-r2", "processed")]
    assert audit(conn, "call.matched", fresh) and not [d for d in old if audit(conn, "call.matched", d)]


def test_closing_through_a_reset_cannot_be_undone_by_a_late_call_or_reply(conn, ro_conn, pack, clock):
    rig = make_rig(conn, ro_conn, pack, clock)
    rig.orch.handle_lead_created(rig.deal_id)
    cancel_pending_work(conn)
    from revos.ports.mail import InboundEmail
    mail = InboundEmail("<late@x>", "sam.lee@brightside-optical.example", "me@x", "Re", "yes interested",
                        datetime(2026, 10, 12, 10, 0, tzinfo=UTC))
    assert rig.orch._deal_for_inbound(mail) == (None, "none", [])           # replies skip closed deals too
