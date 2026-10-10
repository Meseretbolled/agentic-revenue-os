import psycopg
import pytest

from revos.domain.errors import ExitCriteriaNotMet, IllegalTransition, StaleDeal
from revos.domain.stages import DealStage
from revos.orchestrator.transitions import transition_deal


def make_deal(conn, **fields) -> object:
    import json
    return conn.execute(
        "INSERT INTO deals (segment_id, fields) VALUES ('test_seg', %s) RETURNING id",
        (json.dumps(fields),),
    ).fetchone()[0]


def stage_of(conn, deal_id) -> str:
    return conn.execute("SELECT stage FROM deals WHERE id = %s", (deal_id,)).fetchone()[0]


def audit_rows(conn, deal_id) -> list:
    return conn.execute(
        "SELECT actor, event, detail FROM audit_log WHERE deal_id = %s ORDER BY id", (deal_id,)
    ).fetchall()


def test_legal_transition_updates_stage_version_and_audit(conn) -> None:
    deal = make_deal(conn)
    version = transition_deal(conn, deal, DealStage.ENRICHED, actor="enrichment", reason="facts found")
    assert stage_of(conn, deal) == "enriched"
    assert version == 2
    (actor, event, detail), = audit_rows(conn, deal)
    assert (actor, event) == ("enrichment", "deal.stage_changed")
    assert detail == {"from": "new", "to": "enriched", "reason": "facts found", "version": 2}


def test_illegal_transition_fails_and_changes_nothing(conn) -> None:
    deal = make_deal(conn)
    with pytest.raises(IllegalTransition):
        transition_deal(conn, deal, DealStage.QUALIFIED, actor="x", reason="skip")
    assert stage_of(conn, deal) == "new"
    assert audit_rows(conn, deal) == []


def test_audit_row_shares_the_transaction_of_the_stage_change(conn) -> None:
    deal = make_deal(conn)
    with conn.transaction():
        txid = conn.execute("SELECT txid_current()").fetchone()[0]
        transition_deal(conn, deal, DealStage.ENRICHED, actor="a", reason="r")
    audit_txid = conn.execute("SELECT txid FROM audit_log WHERE deal_id = %s", (deal,)).fetchone()[0]
    assert audit_txid == txid


def test_rollback_removes_stage_change_and_audit_together(conn) -> None:
    deal = make_deal(conn)
    with pytest.raises(RuntimeError), conn.transaction():
        transition_deal(conn, deal, DealStage.ENRICHED, actor="a", reason="r")
        raise RuntimeError("something later in the same unit of work failed")
    assert stage_of(conn, deal) == "new"
    assert audit_rows(conn, deal) == []


def test_failing_audit_insert_rolls_back_the_stage_change(conn) -> None:
    deal = make_deal(conn)
    conn.execute("ALTER TABLE audit_log ADD CONSTRAINT boom CHECK (event <> 'deal.stage_changed') NOT VALID")
    try:
        with pytest.raises(psycopg.errors.CheckViolation):
            transition_deal(conn, deal, DealStage.ENRICHED, actor="a", reason="r")
    finally:
        conn.execute("ALTER TABLE audit_log DROP CONSTRAINT boom")
    assert stage_of(conn, deal) == "new"


def test_direct_stage_update_is_refused_by_the_database(conn) -> None:
    deal = make_deal(conn)
    with pytest.raises(psycopg.errors.RaiseException, match="transition_deal"):
        conn.execute("UPDATE deals SET stage = 'enriched' WHERE id = %s", (deal,))


def test_deal_cannot_be_created_past_new(conn) -> None:
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("INSERT INTO deals (segment_id, stage) VALUES ('test_seg', 'qualified')")


def test_exit_criteria_block_the_move_until_fields_exist(conn) -> None:
    deal = make_deal(conn)
    for stage in (DealStage.ENRICHED, DealStage.SCORED, DealStage.OUTREACH, DealStage.REPLIED,
                  DealStage.MEETING):
        transition_deal(conn, deal, stage, actor="t", reason="setup")
    criteria = {DealStage.QUALIFIED: ["budget", "timeline"]}
    with pytest.raises(ExitCriteriaNotMet) as err:
        transition_deal(conn, deal, DealStage.QUALIFIED, actor="q", reason="r", exit_criteria=criteria)
    assert err.value.missing == ["budget", "timeline"]
    conn.execute("UPDATE deals SET fields = %s WHERE id = %s",
                 ('{"budget": "50k", "timeline": "Q1"}', deal))
    transition_deal(conn, deal, DealStage.QUALIFIED, actor="q", reason="r", exit_criteria=criteria)
    assert stage_of(conn, deal) == "qualified"


def test_stale_version_is_rejected(conn) -> None:
    deal = make_deal(conn)
    transition_deal(conn, deal, DealStage.ENRICHED, actor="a", reason="r")
    with pytest.raises(StaleDeal):
        transition_deal(conn, deal, DealStage.SCORED, actor="b", reason="r", expected_version=1)


def test_do_not_contact_is_reachable_and_final(conn) -> None:
    deal = make_deal(conn)
    transition_deal(conn, deal, DealStage.DO_NOT_CONTACT, actor="reply", reason="unsubscribe")
    with pytest.raises(IllegalTransition):
        transition_deal(conn, deal, DealStage.ENRICHED, actor="x", reason="r")


def test_audit_log_is_append_only(conn) -> None:
    deal = make_deal(conn)
    transition_deal(conn, deal, DealStage.ENRICHED, actor="a", reason="r")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        conn.execute("UPDATE audit_log SET actor = 'evil'")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        conn.execute("DELETE FROM audit_log")
