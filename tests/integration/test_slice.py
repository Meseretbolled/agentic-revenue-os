"""lead.created -> Enrichment -> Scoring -> Outreach proposal -> policy decision, offline."""

from revos.domain.stages import DealStage
from tests.pipeline_helpers import make_rig


def audit_events(conn, deal_id) -> list[str]:
    return [r[0] for r in conn.execute("SELECT event FROM audit_log WHERE deal_id = %s ORDER BY id",
                                       (deal_id,))]


def test_the_full_slice_runs_offline_through_the_orchestrator(conn, ro_conn, pack, clock) -> None:
    rig = make_rig(conn, ro_conn, pack, clock)
    result = rig.orch.handle_lead_created(rig.deal_id)

    assert result.status == "action_proposed" and result.runner == "rules"
    assert result.runs == (("enrichment", "completed"), ("scoring", "completed"),
                           ("outreach", "completed"))
    assert result.stage == "scored" and result.decision == "auto"        # tier C first touch: auto
    deal = conn.execute("SELECT stage, tier, total_score, score_reason, fields FROM deals "
                        "WHERE id = %s", (rig.deal_id,)).fetchone()
    assert deal[0] == "scored" and deal[1] == "C" and deal[2] == 38
    assert "Operates 120 locations" in deal[3]
    fields = deal[4]
    assert fields["location_count"] == 120 and fields["industry"] == "optical"
    assert fields["is_franchise"] is True and fields["company_domain"] == "brightside-optical.example"
    assert fields["fact:F1"] == "We operate 120 locations across Texas and Oklahoma."
    assert fields["fact:F2"] == ("Brightside Optical Group is a family of independently owned "
                                 "eye-care practices.")
    assert fields["fact_source:F1"] == "https://brightside-optical.example/"

    action = conn.execute("SELECT type, status, decision, decision_by, payload FROM actions "
                          "WHERE type = 'first_touch'").fetchone()
    assert action[:4] == ("first_touch", "approved", "approved", "policy")
    assert "I noticed that We operate 120 locations across Texas and Oklahoma." in action[4]["body"]
    assert "To stop receiving these emails, reply STOP." in action[4]["body"]
    assert rig.world.mailer.sent == []                 # nothing leaves until the gate runs


def test_audit_log_shows_every_step_and_which_runner_was_used(conn, ro_conn, pack, clock) -> None:
    rig = make_rig(conn, ro_conn, pack, clock)
    rig.orch.handle_lead_created(rig.deal_id)
    events = audit_events(conn, rig.deal_id)
    for expected in ("agent.run_started", "agent.run_completed", "policy.decided",
                     "deal.stage_changed"):
        assert expected in events
    runs = conn.execute("SELECT agent, runner, status, output FROM agent_runs ORDER BY started_at"
                        ).fetchall()
    assert [r[0] for r in runs] == ["enrichment", "scoring", "outreach"]
    assert all(r[1] == "rules" and r[3]["runner"] == "rules" for r in runs)
    started = conn.execute("SELECT detail FROM audit_log WHERE event = 'agent.run_started'"
                           ).fetchall()
    assert {d[0]["runner"] for d in started} == {"rules"}
    assert {d[0]["kind"] for d in started} == {"deterministic", "judgement"}


def test_every_policy_decision_is_audited_with_reasons(conn, ro_conn, pack, clock) -> None:
    rig = make_rig(conn, ro_conn, pack, clock)
    rig.orch.handle_lead_created(rig.deal_id)
    decided = conn.execute("SELECT detail FROM audit_log WHERE event = 'policy.decided' ORDER BY id"
                           ).fetchall()
    assert [d[0]["action_type"] for d in decided] == ["enrich", "score", "first_touch"]
    assert all(d[0]["outcome"] == "auto" for d in decided)


def test_the_approved_first_touch_is_sent_once_by_the_gate_and_moves_the_deal(
        conn, ro_conn, pack, clock) -> None:
    rig = make_rig(conn, ro_conn, pack, clock)
    result = rig.orch.handle_lead_created(rig.deal_id)
    sent = rig.orch.send_action(result.action_id)
    assert sent.status == "sent"
    assert [(m.to, m.subject) for m in rig.world.mailer.sent] == [
        ("sam.lee@brightside-optical.example", "Scaling local campaigns at Brightside Optical Group")]
    assert conn.execute("SELECT stage FROM deals WHERE id = %s", (rig.deal_id,)).fetchone()[0] \
        == DealStage.OUTREACH.value
    assert rig.orch.send_action(result.action_id).status == "sent"
    assert len(rig.world.mailer.sent) == 1             # asking again sends nothing more


def test_the_worker_handlers_drive_the_same_slice(conn, ro_conn, pack, clock) -> None:
    import random

    from revos.runtime.events import record_event
    from revos.runtime.worker import Worker
    rig = make_rig(conn, ro_conn, pack, clock)
    record_event(conn, f"lead:{rig.deal_id}", "lead.created", {"deal_id": str(rig.deal_id)}, clock)
    worker = Worker(rig.orch.handlers(), clock, rng=random.Random(1))
    assert worker.run_once(conn) and worker.run_once(conn)    # lead.created, then action.send
    assert len(rig.world.mailer.sent) == 1
    assert conn.execute("SELECT stage FROM deals").fetchone()[0] == "outreach"
    record_event(conn, f"lead:{rig.deal_id}", "lead.created", {"deal_id": str(rig.deal_id)}, clock)
    assert not worker.run_once(conn)                          # the same webhook again does nothing


def test_a_disqualified_lead_is_closed_and_gets_no_email(conn, ro_conn, pack, clock) -> None:
    rig = make_rig(conn, ro_conn, pack, clock, fields='{"is_competitor": true}')
    result = rig.orch.handle_lead_created(rig.deal_id)
    assert result.status == "closed" and result.runs == (("enrichment", "completed"),
                                                           ("scoring", "completed"))
    assert conn.execute("SELECT stage, tier FROM deals").fetchone() == ("closed_lost", "disqualified")
    assert conn.execute("SELECT count(*) FROM actions WHERE type = 'first_touch'").fetchone()[0] == 0


def test_running_the_pipeline_again_changes_nothing(conn, ro_conn, pack, clock) -> None:
    rig = make_rig(conn, ro_conn, pack, clock)
    first = rig.orch.handle_lead_created(rig.deal_id)
    counts = [conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("agent_runs", "agent_steps", "actions", "audit_log")]
    again = rig.orch.handle_lead_created(rig.deal_id)
    assert again.status == "completed" or again.status == "action_proposed"
    assert conn.execute("SELECT count(*) FROM actions").fetchone()[0] == counts[2] == 3
    assert conn.execute("SELECT count(*) FROM agent_runs").fetchone()[0] == counts[0]
    assert first.action_id is not None
