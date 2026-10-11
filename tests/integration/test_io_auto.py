"""The IO's terms are proposed from the call and the pack's SAMPLE rate card; a person is needed only when
a term falls outside the pack's limits, and `revos io-terms` stays as an audited override."""

import json
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from revos.cli.main import app
from revos.trace import timeline
from tests.integration.test_calls import call, meeting_rig
from tests.integration.test_close_the_deal import qualified, send_all, stage
from tests.integration.test_esign_flow import io_ready as _unused  # noqa: F401
from tests.pipeline_helpers import CONTACT_EMAIL, audit


def lead_record(conn, **extra):
    """The lead record's own facts: the signer's address (never from a call)."""
    conn.execute("UPDATE deals SET fields = fields || %s",
                 (json.dumps({"signer_email": CONTACT_EMAIL, **extra}),))


def test_an_io_is_generated_with_no_manual_terms(conn, ro_conn, pack, clock):
    rig = qualified(conn, ro_conn, pack, clock)                    # the call-001 fixture: $50k for the quarter
    lead_record(conn)
    assert "io_items" not in conn.execute("SELECT fields FROM deals").fetchone()[0]
    result = rig.orch.handle_io(rig.deal_id)
    assert result.status == "action_proposed" and result.decision == "auto"
    fields = conn.execute("SELECT fields FROM deals").fetchone()[0]
    assert fields["io_terms_source"] == "auto" and fields["discount_pct"] == 0
    (row,) = audit(conn, "deal.io_terms_set", rig.deal_id)
    assert row["source"] == "auto" and row["package"] == "growth" and row["discount_pct"] == 0.0
    assert row["monthly_budget_usd"] == pytest.approx(50000 / 3) and "growth chosen" in row["notes"][0]
    assert "fifty thousand dollars" in row["budget_quote"] and row["timeline_quote"]
    assert conn.execute("SELECT actor FROM audit_log WHERE event = 'deal.io_terms_set'").fetchone()[0] == "io-planner"
    doc = conn.execute("SELECT validation FROM documents").fetchone()[0]
    assert doc["ok"] is True
    send_all(rig)
    assert stage(conn) == "out_for_signature" and len(rig.world.esign.requests) == 1
    assert rig.world.esign.requests[0].test_mode is True


def test_the_trace_shows_the_planner_the_package_and_the_validated_io(conn, ro_conn, pack, clock):
    rig = qualified(conn, ro_conn, pack, clock)
    lead_record(conn)
    rig.orch.handle_io(rig.deal_id)
    send_all(rig)
    lines = [e.line() for e in timeline(conn, str(rig.deal_id))]
    text = "\n".join(lines)
    assert any("deal.io_terms_set (by io-planner)" in ln and "source=auto" not in ln for ln in lines) or \
        "deal.io_terms_set (by io-planner)" in text
    assert "agent contract_io ran" in text and "proposed generate_io" in text and "proposed send_io" in text
    assert "policy decided send_io -> auto" in text and "stage proposal -> io_approval" in text
    assert "stage io_approval -> out_for_signature" in text


def test_a_person_is_needed_only_when_a_term_is_outside_the_packs_limits(conn, ro_conn, pack, clock):
    rig = qualified(conn, ro_conn, pack, clock)
    lead_record(conn, budget="Our budget is about $2,000 a month.")
    result = rig.orch.handle_io(rig.deal_id)
    assert result.status == "needs_human" and "below the smallest package" in result.reason
    (row,) = audit(conn, "io.terms_need_a_person", rig.deal_id)
    assert "below the smallest package" in row["reasons"][0]
    assert "io_items" not in conn.execute("SELECT fields FROM deals").fetchone()[0]
    send_all(rig)
    assert rig.world.esign.requests == [] and conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0


def test_a_person_can_still_override_and_the_override_is_audited_and_never_replaced(conn, ro_conn, pack, clock):
    rig = qualified(conn, ro_conn, pack, clock)
    lead_record(conn)
    items = [{"id": "multi_location", "quantity": 120}, {"id": "creative_automation", "quantity": 3}]
    rig.orch.set_io_terms(rig.deal_id, items=items, start="2026-11-02", end="2027-02-01", discount_pct=10,
                          actor="deal-desk")
    rig.orch.handle_io(rig.deal_id)
    fields = conn.execute("SELECT fields FROM deals").fetchone()[0]
    assert fields["io_items"] == items and fields["discount_pct"] == 10 and fields["io_terms_source"] == "override"
    rows = conn.execute("SELECT actor, detail->>'source' FROM audit_log WHERE event = 'deal.io_terms_set'"
                        ).fetchall()
    assert rows == [("deal-desk", "override")]                       # the planner added nothing


def test_the_override_command_is_audited(conn, ro_conn, pack, clock, test_db_url, monkeypatch):
    rig = qualified(conn, ro_conn, pack, clock)
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    result = CliRunner().invoke(app, ["io-terms", str(rig.deal_id), "--items", "multi_location=100,measurement=3",
                                      "--start", "2026-11-02", "--end", "2027-02-01", "--discount", "5"])
    assert result.exit_code == 0, result.output
    assert conn.execute("SELECT actor, detail->>'source' FROM audit_log WHERE event = 'deal.io_terms_set'"
                        ).fetchone() == ("deal-desk", "override")


def test_a_call_that_qualifies_the_deal_carries_it_all_the_way_to_a_validated_io_by_itself(
        conn, ro_conn, pack, clock):
    from revos.domain.stages import DealStage
    from tests.pipeline_helpers import force_stage
    rig = meeting_rig(conn, ro_conn, pack, clock)
    lead_record(conn, location_count=85)
    rig.world.transcripts.add(call())
    rig.orch.poll_calls()
    assert audit(conn, "call.qualification", rig.deal_id)[0]["result"] == "advanced"
    assert audit(conn, "call.io", rig.deal_id)[0]["result"] == "action_proposed"
    assert audit(conn, "deal.io_terms_set", rig.deal_id)[0]["source"] == "auto"
    assert stage(conn) in {"io_approval", "out_for_signature"} and DealStage and force_stage


def test_a_hard_block_term_stays_a_hard_block_even_when_a_person_sets_it(conn, ro_conn, pack, clock):
    rig = qualified(conn, ro_conn, pack, clock)
    lead_record(conn)
    rig.orch.set_io_terms(rig.deal_id, items=[{"id": "measurement", "quantity": 3}], start="2026-11-02",
                          end="2027-02-01", discount_pct=40, actor="deal-desk")
    rig.orch.handle_io(rig.deal_id)
    send_all(rig)
    assert rig.world.esign.requests == []
    assert conn.execute("SELECT status FROM actions WHERE type = 'send_io'").fetchone()[0] == "cancelled"


def test_the_start_date_is_after_today_and_inside_the_timeline(conn, ro_conn, pack, clock):
    clock.set(datetime(2026, 10, 13, 9, 0, tzinfo=UTC))                        # a Tuesday
    rig = qualified(conn, ro_conn, pack, clock)
    lead_record(conn)
    rig.orch.handle_io(rig.deal_id)
    fields = conn.execute("SELECT fields FROM deals").fetchone()[0]
    start = datetime.fromisoformat(fields["io_start_date"])
    assert start.weekday() == 0 and start > clock.now().replace(tzinfo=None)


def test_the_generated_io_agrees_with_the_package_and_the_budget(conn, ro_conn, pack, clock):
    rig = qualified(conn, ro_conn, pack, clock)                    # $50k for the quarter: $16,667 a month, Growth
    lead_record(conn)
    rig.orch.handle_io(rig.deal_id)
    doc_text, check = conn.execute("SELECT body, validation FROM documents").fetchone()
    floor = next(p.min_monthly_budget_usd for p in pack.rate_card.packages if p.id == "growth")
    assert check["ok"] and floor <= check["monthly_subtotal"] <= 50000 / 3
    assert check["months"] == 3 and check["total"] == pytest.approx(check["monthly_subtotal"] * 3)
    assert "Quantities and amounts in the table are PER MONTH." in doc_text
    assert "Number of months: 3" in doc_text and "Term total (for the whole 3-month term): $" in doc_text
    assert f"${check['monthly_subtotal']:,.0f}" in doc_text


def test_an_io_whose_term_total_is_below_the_package_minimum_is_not_sent(conn, ro_conn, pack, clock):
    rig = qualified(conn, ro_conn, pack, clock)
    lead_record(conn, budget="Our budget is about $40,000 a month.")
    short = [{"id": "creative_automation", "quantity": 2}, {"id": "multi_location", "quantity": 85},
             {"id": "measurement", "quantity": 1}]                   # $9,900 a month, $29,700 for 3 months
    rig.orch.set_io_terms(rig.deal_id, items=short, start="2026-11-02", end="2027-02-01", discount_pct=0,
                          actor="deal-desk", package="scale")
    result = rig.orch.handle_io(rig.deal_id)
    send_all(rig)
    assert result.status == "needs_human" and "below the Scale (SAMPLE) minimum" in result.reason
    assert rig.world.esign.requests == [] and stage(conn) != "out_for_signature"
